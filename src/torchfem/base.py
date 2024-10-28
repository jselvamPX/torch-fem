from abc import ABC, abstractmethod

import torch
from tqdm import tqdm

from .materials import Material
from .sparse import sparse_solve


class FEM(ABC):
    def __init__(self, nodes: torch.Tensor, elements: torch.Tensor, material: Material):
        """Initialize a general FEM problem."""

        # Store nodes and elements
        self.nodes = nodes
        self.elements = elements

        # Compute problem size
        self.n_dofs = torch.numel(self.nodes)
        self.n_nod = nodes.shape[0]
        self.n_dim = nodes.shape[1]
        self.n_elem = len(self.elements)

        # Initialize load variables
        self.forces = torch.zeros_like(nodes)
        self.displacements = torch.zeros_like(nodes)
        self.constraints = torch.zeros_like(nodes, dtype=bool)

        # Compute mapping from local to global indices (hard to read, but fast)
        self.idx = (
            (self.n_dim * self.elements).unsqueeze(-1) + torch.arange(self.n_dim)
        ).reshape(self.n_elem, -1)
        idx1 = self.idx.unsqueeze(1).expand(self.n_elem, self.idx.shape[1], -1)
        idx2 = self.idx.unsqueeze(-1).expand(self.n_elem, -1, self.idx.shape[1])
        self.indices = torch.stack([idx1, idx2], dim=0).reshape((2, -1))

        # Vectorize material
        self.material = material.vectorize(self.n_elem)

    @abstractmethod
    def D(self, B):
        pass

    @abstractmethod
    def compute_k(self, detJ, DCD):
        pass

    @abstractmethod
    def compute_f(self, detJ, D, S):
        pass

    def k0(self):
        """Compute element stiffness matrix for zero strain."""
        de = torch.zeros(self.n_int, self.n_elem, self.n_strains)
        ds = torch.zeros(self.n_int, self.n_elem, self.n_strains)
        da = torch.zeros(self.n_int, self.n_elem, self.material.n_state)
        du = torch.zeros_like(self.nodes)
        dde0 = torch.zeros(self.n_elem, self.n_strains)
        self.K = None
        k, _, _, _, _ = self.integrate(de, ds, da, du, dde0)
        return k

    def integrate(self, eps_old, sig_old, sta_old, du, de0):
        """Perform numerical integrations for element stiffness matrix."""
        # Reshape variables
        nodes = self.nodes[self.elements, :]
        du = du.reshape((-1, self.n_dim))[self.elements, :].reshape(self.n_elem, -1)

        # Initialize solution
        eps_new = torch.zeros((self.n_int, self.n_elem, self.n_strains))
        sig_new = torch.zeros((self.n_int, self.n_elem, self.n_strains))
        sta_new = torch.zeros((self.n_int, self.n_elem, self.material.n_state))

        # Initialize nodal force and stiffness
        N_nod = self.etype.nodes
        f = torch.zeros(self.n_elem, self.n_dim * N_nod)
        k = torch.zeros((self.n_elem, self.n_dim * N_nod, self.n_dim * N_nod))

        for i, (w, xi) in enumerate(zip(self.etype.iweights(), self.etype.ipoints())):
            # Compute gradient operators
            b = self.etype.B(xi)
            J = torch.einsum("jk,mkl->mjl", b, nodes)
            detJ = torch.linalg.det(J)
            if torch.any(detJ <= 0.0):
                raise Exception("Negative Jacobian. Check element numbering.")
            B = torch.einsum("jkl,lm->jkm", torch.linalg.inv(J), b)
            D = self.D(B)

            # Evaluate material response
            de = torch.einsum("jkl,jl->jk", D, du) - de0
            eps_new[i], sig_new[i], sta_new[i], ddsdde = self.material.step(
                de, eps_old[i], sig_old[i], sta_old[i]
            )

            # Compute element internal forces
            f += w * self.compute_f(detJ, D, sig_new[i].clone())

            # Compute element stiffness matrix
            if self.K is None or not self.material.n_state == 0:
                DCD = torch.einsum("jkl,jlm,jkn->jmn", ddsdde, D, D)
                k += w * self.compute_k(detJ, DCD)

        return k, f, eps_new, sig_new, sta_new

    def assemble_stiffness(self, k, con):
        """Assemble global stiffness matrix."""

        # Initialize sparse matrix size
        size = (self.n_dofs, self.n_dofs)

        # Ravel indices and values
        indices = self.indices
        values = k.ravel()

        # Eliminate and replace constrained dofs
        con_mask = torch.zeros(indices[0].max() + 1, dtype=torch.bool)
        con_mask[con] = True
        mask = ~(con_mask[indices[0]] | con_mask[indices[1]])
        diag_index = torch.stack((con, con), dim=0)
        diag_value = torch.ones_like(con, dtype=k.dtype)

        # Concatenate
        indices = torch.cat((indices[:, mask], diag_index), dim=1)
        values = torch.cat((values[mask], diag_value), dim=0)

        return torch.sparse_coo_tensor(indices, values, size=size).coalesce()

    def assemble_force(self, f):
        """Assemble global force vector."""

        # Initialize force vector
        F = torch.zeros((self.n_dofs))

        # Ravel indices and values
        indices = self.idx.ravel()
        values = f.ravel()

        return F.index_add_(0, indices, values)

    def solve(
        self,
        increments=[0, 1],
        max_iter=10,
        tol=1e-4,
        verbose=False,
        return_intermediate=False,
        aggregate_integration_points=True,
    ):
        """Solve the FEM problem with the Newton-Raphson method."""
        # Number of increments
        N = len(increments)

        # Indexes of constrained and unconstrained degrees of freedom
        con = torch.nonzero(self.constraints.ravel(), as_tuple=False).ravel()

        # Initialize variables to be computed
        epsilon = torch.zeros(N, self.n_int, self.n_elem, self.n_strains)
        sigma = torch.zeros(N, self.n_int, self.n_elem, self.n_strains)
        state = torch.zeros(N, self.n_int, self.n_elem, self.material.n_state)
        f = torch.zeros(N, self.n_nod, self.n_dim)
        u = torch.zeros(N, self.n_nod, self.n_dim)

        # Initialize global stiffness matrix
        self.K = None

        # Initialize displacement increment
        du = torch.zeros_like(self.nodes).ravel()

        # Incremental loading
        for i in tqdm(range(1, len(increments)), disable=not verbose, desc="Increment"):
            # Increment size
            inc = increments[i] - increments[i - 1]

            # Load increment
            F_ext = increments[i] * self.forces.ravel()
            DU = inc * self.displacements.clone().ravel()
            DE = inc * self.ext_strain

            # Newton-Raphson iterations
            for _ in range(max_iter):
                du[con] = DU[con]

                # Element-wise integration
                k, f_int, epsilon_new, sigma_new, state_new = self.integrate(
                    epsilon[i - 1], sigma[i - 1], state[i - 1], du, DE
                )

                # Assemble global stiffness matrix and internal force vector. (Only
                # reassemble stiffness matrix if state has changed.)
                if not (state_new == state[i - 1]).all() or self.K is None:
                    self.K = self.assemble_stiffness(k, con)
                F_int = self.assemble_force(f_int)

                # Compute residual
                residual = F_int - F_ext
                residual[con] = 0.0
                res_norm = residual.abs().max() / F_int.abs().max()
                if res_norm < tol:
                    break

                # Solve for displacement increment
                du -= sparse_solve(self.K, residual)

            # Update increment
            epsilon[i] = epsilon_new
            sigma[i] = sigma_new
            state[i] = state_new
            f[i] = F_int.reshape((-1, self.n_dim))
            u[i] = u[i - 1] + du.reshape((-1, self.n_dim))

        # Aggregate integration points as mean
        if aggregate_integration_points:
            epsilon = epsilon.mean(dim=1)
            sigma = sigma.mean(dim=1)
            state = state.mean(dim=1)

        if return_intermediate:
            # Return all intermediate values
            return u, f, sigma, epsilon, state
        else:
            # Return only the final values
            return u[-1], f[-1], sigma[-1], epsilon[-1], state[-1]