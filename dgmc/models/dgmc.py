import torch
from torch.nn import Sequential as Seq, Linear as Lin, ReLU
from torch_geometric.utils import to_dense_batch
from torch_geometric.nn.inits import reset
from pykeops.torch import LazyTensor


def masked_softmax(src, mask, dim=-1):
    out = src.masked_fill(~mask, float('-inf'))
    out = torch.softmax(out, dim=dim)
    out = out.masked_fill(~mask, 0)
    return out


def to_sparse(x, mask):
    return x[mask]


def to_dense(x, mask):
    out = x.new_zeros(tuple(mask.size()) + (x.size(-1), ))
    out[mask] = x
    return out


class DGMC(torch.nn.Module):
    def __init__(self, psi_1, psi_2, num_steps, k=-1, detach=False):
        super(DGMC, self).__init__()

        self.psi_1 = psi_1
        self.psi_2 = psi_2
        self.num_steps = num_steps
        self.k = k
        self.detach = detach

        self.mlp = Seq(
            Lin(psi_2.out_channels, psi_2.out_channels),
            ReLU(),
            Lin(psi_2.out_channels, 1),
        )

        self.reset_parameters()

    def reset_parameters(self):
        self.psi_1.reset_parameters()
        self.psi_2.reset_parameters()
        reset(self.mlp)

    def top_k(self, x_s, x_t):
        backend = 'CPU' if x_s.device.type == 'cpu' else 'auto'
        x_s, x_t = LazyTensor(x_s.unsqueeze(-2)), LazyTensor(x_t.unsqueeze(-3))
        S_ij = (-x_s * x_t).sum(dim=-1)
        return S_ij.argKmin(self.k, dim=1, backend=backend)

    def forward(self, x_s, edge_index_s, edge_attr_s, batch_s, x_t,
                edge_index_t, edge_attr_t, batch_t):
        h_s = self.psi_1(x_s, edge_index_s, edge_attr_s)
        h_t = self.psi_1(x_t, edge_index_t, edge_attr_t)

        h_s, h_t = (h_s.detach(), h_t.detach()) if self.detach else (h_s, h_t)

        # TODO: Skip for batch=None?
        h_s, h_s_mask = to_dense_batch(h_s, batch_s, fill_value=float('inf'))
        h_t, h_t_mask = to_dense_batch(h_t, batch_t, fill_value=float('-inf'))

        if self.k < 1:
            # Dense variant:
            S_hat = h_s @ h_t.transpose(-1, -2)
            S_mask = h_s_mask.unsqueeze(-1) & h_t_mask.unsqueeze(-2)
            S_0 = masked_softmax(S_hat, S_mask, dim=-1)

            rnd_size = (h_s.size(0), h_s.size(1), self.psi_2.in_channels)
            for _ in range(self.num_steps):
                S = masked_softmax(S_hat, S_mask, dim=-1)
                r_s = torch.randn(rnd_size, dtype=h_s.dtype, device=h_s.device)
                r_t = S.transpose(-1, -2) @ r_s

                r_s, r_t = to_sparse(r_s, h_s_mask), to_sparse(r_t, h_t_mask)
                o_s = self.psi_2(r_s, edge_index_s, edge_attr_s)
                o_t = self.psi_2(r_t, edge_index_t, edge_attr_t)
                o_s, o_t = to_dense(o_s, h_s_mask), to_dense(o_t, h_t_mask)

                D = o_s.unsqueeze(-2) - o_t.unsqueeze(-3)
                S_hat = S_hat + self.mlp(D).squeeze(-1).masked_fill(~S_mask, 0)

            S_L = masked_softmax(S_hat, S_mask, dim=-1)

            return S_0, S_L
        else:
            # Sparse variant:
            S_idx = self.top_k(h_s, h_t)
            tmp_s = h_s.unsqueeze(-2)
            tmp_t = h_t.unsqueeze(-3).expand(-1, h_s.size(1), -1, -1)
            index = S_idx.unsqueeze(-1).expand(-1, -1, -1, h_t.size(-1))
            S_hat = (tmp_s * torch.gather(tmp_t, 2, index)).sum(dim=-1)
            S_0 = S_hat.softmax(dim=-1)

            for _ in range(self.num_steps):
                pass

            S_L = S_0

            return S_0, S_L, S_idx

    def __repr__(self):
        return ('{}(\n'
                '    psi_1={},\n'
                '    psi_2={},\n'
                '    num_steps={}, k={}\n)').format(self.__class__.__name__,
                                                    self.psi_1, self.psi_2,
                                                    self.num_steps, self.k)
