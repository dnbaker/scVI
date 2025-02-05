# -*- coding: utf-8 -*-
"""Main module."""
from typing import List, Optional, Union, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.distributions import Normal, Poisson, kl_divergence as kl
from torch.nn import ModuleList

from scvi.models.log_likelihood import log_zinb_positive, log_nb_positive
from scvi.models.modules import Encoder
from scvi.models.modules import MultiEncoder, MultiDecoder
from scvi.models.utils import one_hot

torch.backends.cudnn.benchmark = True


class JVAE(nn.Module):
    """Joint Variational auto-encoder

    Implementation of gimVI:
    *A joint model of unpaired data from scRNA-seq and spatial transcriptomics
    for imputing missing gene expression measurements*
    https://arxiv.org/abs/1905.02269

    """

    def __init__(
        self,
        dim_input_list: List[int],
        total_genes: int,
        indices_mappings: List[Union[np.ndarray, slice]],
        reconstruction_losses: List[str],
        model_library_bools: List[bool],
        n_latent: int = 10,
        n_layers_encoder_individual: int = 1,
        n_layers_encoder_shared: int = 1,
        dim_hidden_encoder: int = 128,
        n_layers_decoder_individual: int = 0,
        n_layers_decoder_shared: int = 0,
        dim_hidden_decoder_individual: int = 32,
        dim_hidden_decoder_shared: int = 128,
        dropout_rate_encoder: float = 0.1,
        dropout_rate_decoder: float = 0.3,
        n_batch: int = 0,
        n_labels: int = 0,
        dispersion: str = "gene-batch",
        log_variational: bool = True,
        model_type: str = "gaussian"
    ):
        """

        :param dim_input_list: List of number of input genes for each dataset. If
                the datasets have different sizes, the dataloader will loop on the
                smallest until it reaches the size of the longest one
        :param total_genes: Total number of different genes
        :param indices_mappings: list of mapping the model inputs to the model output
            Eg: [[0,2], [0,1,3,2]] means the first dataset has 2 genes that will be reconstructed at location [0,2]
                                         the second dataset has 4 genes that will be reconstructed at [0,1,3,2]
        :param reconstruction_losses: list of distributions to use in the generative process 'zinb', 'nb', 'poisson'
        :param model_library_bools: bool list: model or not library size with a latent variable or use observed values
        :param n_latent: dimension of latent space
        :param n_layers_encoder_individual: number of individual layers in the encoder
        :param n_layers_encoder_shared: number of shared layers in the encoder
        :param dim_hidden_encoder: dimension of the hidden layers in the encoder
        :param n_layers_decoder_individual: number of layers that are conditionally batchnormed in the encoder
        :param n_layers_decoder_shared: number of shared layers in the decoder
        :param dim_hidden_decoder_individual: dimension of the individual hidden layers in the decoder
        :param dim_hidden_decoder_shared: dimension of the shared hidden layers in the decoder
        :param dropout_rate_encoder: dropout encoder
        :param dropout_rate_decoder: dropout decoder
        :param n_batch: total number of batches
        :param n_labels: total number of labels
        :param dispersion: See ``vae.py``
        :param log_variational: Log(data+1) prior to encoding for numerical stability. Not normalization.
        """
        super().__init__()

        self.n_input_list = dim_input_list
        self.total_genes = total_genes
        self.indices_mappings = indices_mappings
        self.reconstruction_losses = reconstruction_losses
        self.model_library_bools = model_library_bools
        self.model_type = model_type

        self.n_latent = n_latent

        self.n_batch = n_batch
        self.n_labels = n_labels

        self.dispersion = dispersion
        self.log_variational = log_variational

        self.z_encoder = MultiEncoder(
            n_heads=len(dim_input_list),
            n_input_list=dim_input_list,
            n_output=self.n_latent,
            n_hidden=dim_hidden_encoder,
            n_layers_individual=n_layers_encoder_individual,
            n_layers_shared=n_layers_encoder_shared,
            dropout_rate=dropout_rate_encoder,
            model_type = model_type
        )

        self.l_encoders = ModuleList(
            [
                Encoder(
                    self.n_input_list[i],
                    1,
                    n_layers=1,
                    dropout_rate=dropout_rate_encoder,
                )
                if self.model_library_bools[i]
                else None
                for i in range(len(self.n_input_list))
            ]
        )

        self.decoder = MultiDecoder(
            self.n_latent,
            self.total_genes,
            n_hidden_conditioned=dim_hidden_decoder_individual,
            n_hidden_shared=dim_hidden_decoder_shared,
            n_layers_conditioned=n_layers_decoder_individual,
            n_layers_shared=n_layers_decoder_shared,
            n_cat_list=[self.n_batch],
            dropout_rate=dropout_rate_decoder,
        )

        if self.dispersion == "gene":
            self.px_r = torch.nn.Parameter(torch.randn(self.total_genes))
        elif self.dispersion == "gene-batch":
            self.px_r = torch.nn.Parameter(torch.randn(self.total_genes, n_batch))
        elif self.dispersion == "gene-label":
            self.px_r = torch.nn.Parameter(torch.randn(self.total_genes, n_labels))
        else:  # gene-cell
            pass

    def sample_from_posterior_z(
        self, x: torch.Tensor, mode: int = None, deterministic: bool = False
    ) -> torch.Tensor:
        """Sample tensor of latent values from the posterior

        :param x: tensor of values with shape ``(batch_size, n_input)``
        :param mode: head id to use in the encoder
        :param deterministic: bool - whether to sample or not
        :return: tensor of shape ``(batch_size, n_latent)``
        """
        if mode is None:
            if len(self.n_input_list) == 1:
                mode = 0
            else:
                raise Exception("Must provide a mode when having multiple datasets")
        qz_m, _, z, _, _, _ = self.encode(x, mode)
        if deterministic:
            z = qz_m
        return z

    def sample_from_posterior_l(
        self, x: torch.Tensor, mode: int = None, deterministic: bool = False
    ) -> torch.Tensor:
        """Sample the tensor of library sizes from the posterior

        :param x: tensor of values with shape ``(batch_size, n_input)``
        or ``(batch_size, n_input_fish)`` depending on the mode
        :param mode: head id to use in the encoder
        :param deterministic: bool - whether to sample or not
        :return: tensor of shape ``(batch_size, 1)``
        """
        _, _, _, ql_m, _, library = self.encode(x, mode)
        if deterministic and ql_m is not None:
            library = ql_m
        return library

    def sample_scale(
        self,
        x: torch.Tensor,
        mode: int,
        batch_index: torch.Tensor,
        y: Optional[torch.Tensor] = None,
        deterministic: bool = False,
        decode_mode: Optional[int] = None,
    ) -> torch.Tensor:
        """Return the tensor of predicted frequencies of expression

        :param x: tensor of values with shape ``(batch_size, n_input)``
        or ``(batch_size, n_input_fish)`` depending on the mode
        :param mode: int encode mode (which input head to use in the model)
        :param batch_index: array that indicates which batch the cells belong to with shape ``batch_size``
        :param y: tensor of cell-types labels with shape ``(batch_size, n_labels)``
        :param deterministic: bool - whether to sample or not
        :param decode_mode: int use to a decode mode different from encoding mode
        :return: tensor of predicted expression
        """
        if decode_mode is None:
            decode_mode = mode
        qz_m, qz_v, z, ql_m, ql_v, library = self.encode(x, mode)
        if deterministic:
            z = qz_m
            if ql_m is not None:
                library = ql_m
        px_scale, px_r, px_rate, px_dropout = self.decode(
            z, decode_mode, library, batch_index, y
        )

        return px_scale

    # This is a potential wrapper for a vae like get_sample_rate
    def get_sample_rate(self, x, batch_index, *_, **__):
        return self.sample_rate(x, 0, batch_index)

    def sample_rate(
        self,
        x: torch.Tensor,
        mode: int,
        batch_index: torch.Tensor,
        y: Optional[torch.Tensor] = None,
        deterministic: bool = False,
        decode_mode: int = None,
    ) -> torch.Tensor:
        """Returns the tensor of scaled frequencies of expression

        :param x: tensor of values with shape ``(batch_size, n_input)``
        or ``(batch_size, n_input_fish)`` depending on the mode
        :param y: tensor of cell-types labels with shape ``(batch_size, n_labels)``
        :param mode: int encode mode (which input head to use in the model)
        :param batch_index: array that indicates which batch the cells belong to with shape ``batch_size``
        :param deterministic: bool - whether to sample or not
        :param decode_mode: int use to a decode mode different from encoding mode
        :return: tensor of means of the scaled frequencies
        """
        if decode_mode is None:
            decode_mode = mode
        qz_m, qz_v, z, ql_m, ql_v, library = self.encode(x, mode)
        if deterministic:
            z = qz_m
            if ql_m is not None:
                library = ql_m
        px_scale, px_r, px_rate, px_dropout = self.decode(
            z, decode_mode, library, batch_index, y
        )

        return px_rate

    def reconstruction_loss(
        self,
        x: torch.Tensor,
        px_rate: torch.Tensor,
        px_r: torch.Tensor,
        px_dropout: torch.Tensor,
        mode: int,
    ) -> torch.Tensor:
        reconstruction_loss = None
        if self.reconstruction_losses[mode] == "zinb":
            reconstruction_loss = -log_zinb_positive(x, px_rate, px_r, px_dropout)
        elif self.reconstruction_losses[mode] == "nb":
            reconstruction_loss = -log_nb_positive(x, px_rate, px_r)
        elif self.reconstruction_losses[mode] == "poisson":
            reconstruction_loss = -torch.sum(Poisson(px_rate).log_prob(x), dim=1)
        return reconstruction_loss

    def encode(
        self, x: torch.Tensor, mode: int
    ) -> Tuple[
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        Optional[torch.Tensor],
        Optional[torch.Tensor],
        torch.Tensor,
    ]:
        x_ = x
        if self.log_variational:
            x_ = torch.log(1 + x_)

        qz_m, qz_v, z = self.z_encoder(x_, mode)
        ql_m, ql_v, library = None, None, None
        if self.model_library_bools[mode]:
            ql_m, ql_v, library = self.l_encoders[mode](x_)
        else:
            library = torch.log(torch.sum(x, dim=1)).view(-1, 1)

        return qz_m, qz_v, z, ql_m, ql_v, library

    def decode(
        self,
        z: torch.Tensor,
        mode: int,
        library: torch.Tensor,
        batch_index: Optional[torch.Tensor] = None,
        y: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        px_scale, px_r, px_rate, px_dropout = self.decoder(
            z, mode, library, self.dispersion, batch_index, y
        )

        if self.dispersion == "gene-label":
            px_r = F.linear(one_hot(y, self.n_labels), self.px_r)
        elif self.dispersion == "gene-batch":
            px_r = F.linear(one_hot(batch_index, self.n_batch), self.px_r)
        elif self.dispersion == "gene":
            px_r = self.px_r.view(1, self.px_r.size(0))
        px_r = torch.exp(px_r)

        px_scale = px_scale / torch.sum(
            px_scale[:, self.indices_mappings[mode]], dim=1
        ).view(-1, 1)
        px_rate = px_scale * torch.exp(library)

        return px_scale, px_r, px_rate, px_dropout

    def forward(
        self,
        x: torch.Tensor,
        local_l_mean: torch.Tensor,
        local_l_var: torch.Tensor,
        batch_index: Optional[torch.Tensor] = None,
        y: Optional[torch.Tensor] = None,
        mode: Optional[int] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Return the reconstruction loss and the Kullback divergences

        :param x: tensor of values with shape ``(batch_size, n_input)``
        or ``(batch_size, n_input_fish)`` depending on the mode
        :param local_l_mean: tensor of means of the prior distribution of latent variable l
        with shape (batch_size, 1)
        :param local_l_var: tensor of variances of the prior distribution of latent variable l
        with shape (batch_size, 1)
        :param batch_index: array that indicates which batch the cells belong to with shape ``batch_size``
        :param y: tensor of cell-types labels with shape (batch_size, n_labels)
        :param mode: indicates which head/tail to use in the joint network
        :return: the reconstruction loss and the Kullback divergences
        """
        if mode is None:
            if len(self.n_input_list) == 1:
                mode = 0
            else:
                raise Exception("Must provide a mode")

        qz_m, qz_v, z, ql_m, ql_v, library = self.encode(x, mode)
        px_scale, px_r, px_rate, px_dropout = self.decode(
            z, mode, library, batch_index, y
        )

        # mask loss to observed genes
        mapping_indices = self.indices_mappings[mode]
        reconstruction_loss = self.reconstruction_loss(
            x,
            px_rate[:, mapping_indices],
            px_r[:, mapping_indices],
            px_dropout[:, mapping_indices],
            mode,
        )

        # KL Divergence
        mean = torch.zeros_like(qz_m)
        scale = torch.ones_like(qz_v)
        kl_divergence_z = kl(Normal(qz_m, torch.sqrt(qz_v)), Normal(mean, scale)).sum(
            dim=1
        )

        if self.model_library_bools[mode]:
            kl_divergence_l = kl(
                Normal(ql_m, torch.sqrt(ql_v)),
                Normal(local_l_mean, torch.sqrt(local_l_var)),
            ).sum(dim=1)
        else:
            kl_divergence_l = torch.zeros_like(kl_divergence_z)

        return reconstruction_loss, kl_divergence_l + kl_divergence_z
