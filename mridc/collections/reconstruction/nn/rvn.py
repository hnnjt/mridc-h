# coding=utf-8
__author__ = "Dimitrios Karkalousos"

import math
from abc import ABC
from typing import Optional

import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer

import mridc.collections.reconstruction.nn.base as base_models
import mridc.core.classes.common as common_classes
from mridc.collections.common.parts import fft, utils
from mridc.collections.reconstruction.nn.recurrentvarnet import recurrentvarnet

__all__ = ["RecurrentVarNet"]


class RecurrentVarNet(base_models.BaseMRIReconstructionModel, ABC):  # type: ignore
    """
    Implementation of the Recurrent Variational Network implementation, as presented in [1].

    References
    ----------
    .. [1] Yiasemis, George, et al. “Recurrent Variational Network: A Deep Learning Inverse Problem Solver Applied to
        the Task of Accelerated MRI Reconstruction.” ArXiv:2111.09639 [Physics], Nov. 2021. arXiv.org,
        http://arxiv.org/abs/2111.09639.
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        super().__init__(cfg=cfg, trainer=trainer)

        cfg_dict = OmegaConf.to_container(cfg, resolve=True)

        self.in_channels = cfg_dict.get("in_channels")
        self.recurrent_hidden_channels = cfg_dict.get("recurrent_hidden_channels")
        self.recurrent_num_layers = cfg_dict.get("recurrent_num_layers")
        self.no_parameter_sharing = cfg_dict.get("no_parameter_sharing")

        # make time-steps size divisible by 8 for fast fp16 training
        self.num_steps = 8 * math.ceil(cfg_dict.get("num_steps") / 8)

        self.learned_initializer = cfg_dict.get("learned_initializer")
        self.initializer_initialization = cfg_dict.get("initializer_initialization")
        self.initializer_channels = cfg_dict.get("initializer_channels")
        self.initializer_dilations = cfg_dict.get("initializer_dilations")

        if (
            self.learned_initializer
            and self.initializer_initialization is not None
            and self.initializer_channels is not None
            and self.initializer_dilations is not None
        ):
            if self.initializer_initialization not in [
                "sense",
                "input_image",
                "zero_filled",
            ]:
                raise ValueError(
                    "Unknown initializer_initialization. Expected `sense`, `'input_image` or `zero_filled`."
                    f"Got {self.initializer_initialization}."
                )
            self.initializer = recurrentvarnet.RecurrentInit(
                self.in_channels,
                self.recurrent_hidden_channels,
                channels=self.initializer_channels,
                dilations=self.initializer_dilations,
                depth=self.recurrent_num_layers,
                multiscale_depth=cfg_dict.get("initializer_multiscale"),
            )
        else:
            self.initializer = None  # type: ignore

        self.block_list: torch.nn.Module = torch.nn.ModuleList()
        for _ in range(self.num_steps if self.no_parameter_sharing else 1):
            self.block_list.append(
                recurrentvarnet.RecurrentVarNetBlock(
                    in_channels=self.in_channels,
                    hidden_channels=self.recurrent_hidden_channels,
                    num_layers=self.recurrent_num_layers,
                    fft_centered=self.fft_centered,
                    fft_normalization=self.fft_normalization,
                    spatial_dims=self.spatial_dims,
                    coil_dim=self.coil_dim,
                )
            )

        std_init_range = 1 / self.recurrent_hidden_channels**0.5

        # initialize weights if not using pretrained cirim
        if not cfg_dict.get("pretrained", False):
            self.block_list.apply(lambda module: utils.rnn_weights_init(module, std_init_range))

    @common_classes.typecheck()  # type: ignore
    def forward(  # noqa: W0221
        self,
        y: torch.Tensor,
        sensitivity_maps: torch.Tensor,
        mask: torch.Tensor,
        init_pred: torch.Tensor,  # noqa: W0613
        target: torch.Tensor,
        **kwargs,
    ) -> torch.Tensor:
        """
        Forward pass of the network.

        Parameters
        ----------
        y : torch.Tensor
            Subsampled k-space data. Shape [batch_size, n_coils, n_x, n_y, 2]
        sensitivity_maps : torch.Tensor
            Coil sensitivity maps. Shape [batch_size, n_coils, n_x, n_y, 2]
        mask : torch.Tensor
            Subsampling mask. Shape [1, 1, n_x, n_y, 1]
        init_pred : torch.Tensor
            Initial prediction. Shape [batch_size, n_x, n_y, 2]
        target : torch.Tensor
            Target data to compute the loss. Shape [batch_size, n_x, n_y, 2]

        Returns
        -------
        torch.Tensor
            Reconstructed image. Shape [batch_size, n_x, n_y, 2]
        """
        previous_state: Optional[torch.Tensor] = None

        if self.initializer is not None:
            if self.initializer_initialization == "sense":
                initializer_input_image = (
                    utils.complex_mul(
                        fft.ifft2(
                            y,
                            centered=self.fft_centered,
                            normalization=self.fft_normalization,
                            spatial_dims=self.spatial_dims,
                        ),
                        utils.complex_conj(sensitivity_maps),
                    )
                    .sum(self.coil_dim)
                    .unsqueeze(self.coil_dim)
                )
            elif self.initializer_initialization == "input_image":
                if "initial_image" not in kwargs:
                    raise ValueError(
                        "`'initial_image` is required as input if initializer_initialization "
                        f"is {self.initializer_initialization}."
                    )
                initializer_input_image = kwargs["initial_image"].unsqueeze(self.coil_dim)
            elif self.initializer_initialization == "zero_filled":
                initializer_input_image = fft.ifft2(
                    y,
                    centered=self.fft_centered,
                    normalization=self.fft_normalization,
                    spatial_dims=self.spatial_dims,
                )

            previous_state = self.initializer(
                fft.fft2(
                    initializer_input_image,
                    centered=self.fft_centered,
                    normalization=self.fft_normalization,
                    spatial_dims=self.spatial_dims,
                )
                .sum(1)
                .permute(0, 3, 1, 2)
            )

        kspace_prediction = y.clone()

        for step in range(self.num_steps):
            block = self.block_list[step] if self.no_parameter_sharing else self.block_list[0]
            kspace_prediction, previous_state = block(
                kspace_prediction,
                y,
                mask,
                sensitivity_maps,
                previous_state,
            )

        prediction = fft.ifft2(
            kspace_prediction,
            centered=self.fft_centered,
            normalization=self.fft_normalization,
            spatial_dims=self.spatial_dims,
        )
        prediction = utils.coil_combination_method(
            prediction, sensitivity_maps, method=self.coil_combination_method, dim=self.coil_dim
        )
        prediction = torch.view_as_complex(prediction)
        if target.shape[-1] == 2:
            target = torch.view_as_complex(target)
        _, prediction = utils.center_crop_to_smallest(target, prediction)
        return prediction
