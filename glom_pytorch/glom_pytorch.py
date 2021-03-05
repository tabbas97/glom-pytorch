import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops import rearrange, repeat
from einops.layers.torch import Rearrange

# constants

TOKEN_ATTEND_SELF_VALUE = -5e-4

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# class

class GroupedFeedForward(nn.Module):
    def __init__(self, *, dim, groups, mult = 4):
        super().__init__()
        self.project_in  = nn.Parameter(torch.randn(groups, dim, dim * 4))
        self.nonlin      = nn.GELU()
        self.project_out = nn.Parameter(torch.randn(groups, dim * 4, dim))

    def forward(self, levels):
        x = einsum('b n l d, l d e -> b n l e', levels, self.project_in)
        x = self.nonlin(x)
        x = einsum('b n l d, l d e -> b n l e', x, self.project_out)
        return x

class ConsensusAttention(nn.Module):
    def __init__(self, attend_self = True):
        super().__init__()
        self.attend_self = attend_self

    def forward(self, levels):
        _, n, _, d, device = *levels.shape, levels.device
        q, k, v = levels, F.normalize(levels, dim = -1), levels

        sim = einsum('b i l d, b j l d -> b l i j', q, k) * (d ** -0.5)

        if not self.attend_self:
            self_mask = torch.eye(n, device = device, dtype = torch.bool)
            self_mask = rearrange(self_mask, 'i j -> () () i j')
            sim.masked_fill_(self_mask, TOKEN_ATTEND_SELF_VALUE)

        attn = sim.softmax(dim = -1)
        out = einsum('b l i j, b j l d -> b i l d', attn, levels)
        return out

# main class

class Glom(nn.Module):
    def __init__(
        self,
        *,
        dim = 512,
        levels = 6,
        image_size = 224,
        patch_size = 14,
        consensus_self = False
    ):
        super().__init__()
        # bottom level - incoming image, tokenize and add position
        num_patches = (image_size // patch_size) ** 2
        self.levels = levels

        self.image_to_tokens = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1 = patch_size, p2 = patch_size),
            nn.Linear(patch_size ** 2 * 3, dim)
        )
        self.pos_emb = nn.Embedding(num_patches, dim)

        # initial embeddings for all levels of a column
        self.init_levels = nn.Parameter(torch.randn(levels, dim))

        # bottom-up and top-down
        self.bottom_up = GroupedFeedForward(dim = dim, groups = levels - 1)
        self.top_down = GroupedFeedForward(dim = dim, groups = levels - 1)

        # consensus attention
        self.attention = ConsensusAttention(attend_self = consensus_self)

    def forward(self, img, iters = None, return_all = False):
        b, device = img.shape[0], img.device
        iters = default(iters, self.levels * 2)   # need to have twice the number of levels of iterations in order for information to propagate up and back down. can be overridden

        tokens = self.image_to_tokens(img)
        n = tokens.shape[1]

        pos_embs = self.pos_emb(torch.arange(n, device = device))
        bottom_level = tokens + rearrange(pos_embs, 'n d -> () n d')
        bottom_level = rearrange(bottom_level, 'b n d -> b n () d')

        levels = repeat(self.init_levels, 'l d -> b n l d', b = b, n = n)

        hiddens = [levels]

        for _ in range(iters):
            levels_with_input = torch.cat((bottom_level, levels), dim = -2)  # each iteration, attach original input (with positional embedding) at the bottom level

            bottom_up_out = self.bottom_up(levels_with_input[..., 1:-1, :])
            top_down_out = self.top_down(levels_with_input[..., 2:, :])

            bottom_up_out = torch.cat((bottom_level, bottom_up_out), dim = -2)
            top_down_out = F.pad(top_down_out, (0, 0, 0, 1), value = 0.)

            consensus = self.attention(levels)

            levels = torch.stack((levels, bottom_up_out, top_down_out, consensus)).mean(dim = 0) # hinton said to use the weighted mean of (1) bottom up (2) top down (3) previous level value {t - 1} (4) consensus value
            hiddens.append(levels)

        if return_all:
            return torch.stack(hiddens)  # return (time step, batch, num columns, levels, dimension)

        return levels
