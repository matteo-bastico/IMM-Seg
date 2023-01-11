# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from typing import Sequence, Union, Tuple

import torch
import torch.nn as nn

from monai.utils import optional_import
from monai.networks.blocks.patchembedding import PatchEmbeddingBlock
from ..blocks.transformer_block import TransformerBlock
from ..layers.utils import get_norm_layer

rearrange, _ = optional_import("einops", name="rearrange")


__all__ = ["ViT"]


class ViT(nn.Module):
    """
    Vision Transformer (ViT), based on: "Dosovitskiy et al.,
    An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale <https://arxiv.org/abs/2010.11929>"

    ViT supports Torchscript but only works for Pytorch after 1.8.
    """

    def __init__(
        self,
        in_channels: int,
        img_size: Union[Sequence[int], int],
        patch_size: Union[Sequence[int], int],
        hidden_size: int = 768,
        mlp_dim: int = 3072,
        num_layers: int = 12,
        num_heads: int = 12,
        pos_embed: str = "conv",
        classification: bool = False,
        num_classes: int = 2,
        dropout_rate: float = 0.0,
        spatial_dims: int = 3,
        post_activation="Tanh",
        qkv_bias: bool = False,
        norm_type: Union[Tuple, str] = "layer",
    ) -> None:
        """
        Args:
            in_channels: dimension of input channels.
            img_size: dimension of input image.
            patch_size: dimension of patch size.
            hidden_size: dimension of hidden layer.
            mlp_dim: dimension of feedforward layer.
            num_layers: number of transformer blocks.
            num_heads: number of attention heads.
            pos_embed: position embedding layer type.
            classification: bool argument to determine if classification is used.
            num_classes: number of classes if classification is used.
            dropout_rate: faction of the input units to drop.
            spatial_dims: number of spatial dimensions.
            post_activation: add a final acivation function to the classification head when `classification` is True.
                Default to "Tanh" for `nn.Tanh()`. Set to other values to remove this function.
            qkv_bias: apply bias to the qkv linear layer in self attention block
            norm_type: feature normalization type and arguments for decoder layers

        Examples::

            # for single channel input with image size of (96,96,96), conv position embedding and segmentation backbone
            >>> net = ViT(in_channels=1, img_size=(96,96,96), pos_embed='conv')

            # for 3-channel with image size of (128,128,128), 24 layers and classification backbone
            >>> net = ViT(in_channels=3, img_size=(128,128,128), pos_embed='conv', classification=True)

            # for 3-channel with image size of (224,224), 12 layers and classification backbone
            >>> net = ViT(in_channels=3, img_size=(224,224), pos_embed='conv', classification=True, spatial_dims=2)

        """

        super().__init__()

        if not (0 <= dropout_rate <= 1):
            raise ValueError("dropout_rate should be between 0 and 1.")

        if hidden_size % num_heads != 0:
            raise ValueError("hidden_size should be divisible by num_heads.")
        # Save norm_type from argument
        self.norm_type = norm_type[0] if isinstance(norm_type, Tuple) else norm_type
        self.classification = classification
        self.patch_embedding = PatchEmbeddingBlock(
            in_channels=in_channels,
            img_size=img_size,
            patch_size=patch_size,
            hidden_size=hidden_size,
            num_heads=num_heads,
            pos_embed=pos_embed,
            dropout_rate=dropout_rate,
            spatial_dims=spatial_dims,
        )
        self.blocks = nn.ModuleList(
            [TransformerBlock(hidden_size,
                              mlp_dim,
                              num_heads,
                              dropout_rate,
                              qkv_bias,
                              norm_type=norm_type) for i in range(num_layers)]
        )

        # Automatic adding normalized_shape for layer normalization
        if self.norm_type == "layer":
            if isinstance(norm_type, Tuple):
                norm_type[1]["normalized_shape"] = hidden_size
            else:
                norm_type = (norm_type, {"normalized_shape": hidden_size})

        # spatial_dims is 1 because (B, N_p, F)
        self.norm = get_norm_layer(name=norm_type,
                                   spatial_dims=1,
                                   channels=hidden_size)

        if self.classification:
            self.cls_token = nn.Parameter(torch.zeros(1, 1, hidden_size))
            if post_activation == "Tanh":
                self.classification_head = nn.Sequential(nn.Linear(hidden_size, num_classes), nn.Tanh())
            else:
                self.classification_head = nn.Linear(hidden_size, num_classes)  # type: ignore

    def forward(self,
                x,
                modalities=None):

        if self.norm_type == "instance_cond" and modalities is None:
            raise ValueError("Modalities must be passed to the forward step when encoder_norm_type is 'instance_cond'.")

        x = self.patch_embedding(x)
        if hasattr(self, "cls_token"):
            cls_token = self.cls_token.expand(x.shape[0], -1, -1)
            x = torch.cat((cls_token, x), dim=1)
        hidden_states_out = []

        for blk in self.blocks:
            x = blk(x, modalities)
            hidden_states_out.append(x)
        # Normalize
        if self.norm_type == "layer":
            x = self.norm(x)
        else:
            # All other norms types need rearrange
            x = rearrange(x, "n l c -> n c l")
            if self.norm_type == "instance_cond":
                x = self.norm(x, modalities)
            else:
                x = self.norm(x)
            x = rearrange(x, "n c l -> n l c")

        if hasattr(self, "classification_head"):
            x = self.classification_head(x[:, 0])
        return x, hidden_states_out
