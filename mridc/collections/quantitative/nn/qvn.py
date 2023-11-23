# coding=utf-8
__author__ = "Dimitrios Karkalousos"

from abc import ABC
from typing import Any, List, Union

import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer
from torch import Tensor

import mridc.collections.quantitative.nn.base as base_quantitative_models
import mridc.collections.quantitative.nn.qrim.utils as qrim_utils
import mridc.core.classes.common as common_classes
from mridc.collections.common.parts import fft, utils
from mridc.collections.quantitative.nn.qvarnet import qvn_block
from mridc.collections.quantitative.parts import transforms
from mridc.collections.reconstruction.nn.unet_base import unet_block

__all__ = ["qVarNet"]


class qVarNet(base_quantitative_models.BaseqMRIReconstructionModel, ABC):  # type: ignore
    """
    Implementation of the quantitative End-to-end Variational Network (qVN), as presented in [1].

    References
    ----------
    .. [1] Zhang C, Karkalousos D, Bazin PL, Coolen BF, Vrenken H, Sonke JJ, Forstmann BU, Poot DH, Caan MW. A unified
        model for reconstruction and R2* mapping of accelerated 7T data using the quantitative recurrent inference
        machine. NeuroImage. 2022 Dec 1;264:119680.
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        # init superclass
        super().__init__(cfg=cfg, trainer=trainer)

        cfg_dict = OmegaConf.to_container(cfg, resolve=True)
        quantitative_module_dimensionality = cfg_dict.get("quantitative_module_dimensionality")
        if quantitative_module_dimensionality != 2:
            raise ValueError(
                f"Only 2D is currently supported for qMRI models.Found {quantitative_module_dimensionality}"
            )

        self.shift_B0_input = cfg_dict.get("shift_B0_input")

        self.vn = torch.nn.ModuleList([])

        self.use_reconstruction_module = cfg_dict.get("use_reconstruction_module")
        if self.use_reconstruction_module:
            self.reconstruction_module_num_cascades = cfg_dict.get("reconstruction_module_num_cascades")
            self.reconstruction_module_no_dc = cfg_dict.get("reconstruction_module_no_dc")

            for _ in range(self.reconstruction_module_num_cascades):
                self.vn.append(
                    qvn_block.qVarNetBlock(
                        unet_block.NormUnet(
                            chans=cfg_dict.get("reconstruction_module_channels"),
                            num_pools=cfg_dict.get("reconstruction_module_pooling_layers"),
                            in_chans=cfg_dict.get("reconstruction_module_in_channels"),
                            out_chans=cfg_dict.get("reconstruction_module_out_channels"),
                            padding_size=cfg_dict.get("reconstruction_module_padding_size"),
                            normalize=cfg_dict.get("reconstruction_module_normalize"),
                        ),
                        fft_centered=self.fft_centered,
                        fft_normalization=self.fft_normalization,
                        spatial_dims=self.spatial_dims,
                        coil_dim=self.coil_dim - 1,
                        no_dc=self.reconstruction_module_no_dc,
                    )
                )

            self.dc_weight = torch.nn.Parameter(torch.ones(1))
            self.reconstruction_module_accumulate_predictions = cfg_dict.get(
                "reconstruction_module_accumulate_predictions"
            )

        quantitative_module_num_cascades = cfg_dict.get("quantitative_module_num_cascades")
        self.qvn = torch.nn.ModuleList(
            [
                qvn_block.qVarNetBlock(
                    unet_block.NormUnet(
                        chans=cfg_dict.get("quantitative_module_channels"),
                        num_pools=cfg_dict.get("quantitative_module_pooling_layers"),
                        in_chans=cfg_dict.get("quantitative_module_in_channels"),
                        out_chans=cfg_dict.get("quantitative_module_out_channels"),
                        padding_size=cfg_dict.get("quantitative_module_padding_size"),
                        normalize=cfg_dict.get("quantitative_module_normalize"),
                    ),
                    fft_centered=self.fft_centered,
                    fft_normalization=self.fft_normalization,
                    spatial_dims=self.spatial_dims,
                    coil_dim=self.coil_dim,
                    no_dc=cfg_dict.get("quantitative_module_no_dc"),
                    linear_forward_model=base_quantitative_models.SignalForwardModel(
                        sequence=cfg_dict.get("quantitative_module_signal_forward_model_sequence")
                    ),
                )
                for _ in range(quantitative_module_num_cascades)
            ]
        )

        self.accumulate_predictions = cfg_dict.get("quantitative_module_accumulate_predictions")

        self.gamma = torch.tensor(cfg_dict.get("quantitative_module_gamma_regularization_factors"))
        self.preprocessor = qrim_utils.RescaleByMax

    @common_classes.typecheck()  # type: ignore
    def forward(  # noqa: W0221
        self,
        R2star_map_init: torch.Tensor,
        S0_map_init: torch.Tensor,
        B0_map_init: torch.Tensor,
        phi_map_init: torch.Tensor,
        TEs: List,
        y: torch.Tensor,
        sensitivity_maps: torch.Tensor,
        mask_brain: torch.Tensor,
        sampling_mask: torch.Tensor,
    ) -> List[Union[Tensor, List[Any]]]:
        """
        Forward pass of the network.

        Parameters
        ----------
        R2star_map_init : torch.Tensor
            Initial R2* map of shape [batch_size, n_x, n_y].
        S0_map_init : torch.Tensor
            Initial S0 map of shape [batch_size, n_x, n_y].
        B0_map_init : torch.Tensor
            Initial B0 map of shape [batch_size, n_x, n_y].
        phi_map_init : torch.Tensor
            Initial phase map of shape [batch_size, n_x, n_y].
        TEs : List
            List of echo times.
        y : torch.Tensor
            Subsampled k-space data of shape [batch_size, n_echoes, n_coils, n_x, n_y, 2].
        sensitivity_maps : torch.Tensor
            Coil sensitivity maps of shape [batch_size, n_coils, n_x, n_y, 2].
        mask_brain : torch.Tensor
            Brain mask of shape [batch_size, 1, n_x, n_y, 1].
        sampling_mask : torch.Tensor
            Sampling mask of shape [batch_size, 1, n_x, n_y, 1].

        Returns
        -------
        List of list of torch.Tensor or torch.Tensor
             If self.accumulate_loss is True, returns a list of all intermediate predictions.
             If False, returns the final estimate.
        """
        if self.use_reconstruction_module:
            cascades_echoes_predictions = []
            for echo in range(y.shape[1]):
                prediction = y[:, echo, ...].clone()
                for cascade in self.vn:
                    # Forward pass through the cascades
                    prediction = cascade(prediction, y[:, echo, ...], sensitivity_maps, sampling_mask.squeeze(1))
                estimation = fft.ifft2(
                    prediction,
                    centered=self.fft_centered,
                    normalization=self.fft_normalization,
                    spatial_dims=self.spatial_dims,
                )
                estimation = utils.coil_combination_method(
                    estimation, sensitivity_maps, method=self.coil_combination_method, dim=self.coil_dim - 1
                )
                cascades_echoes_predictions.append(torch.view_as_complex(estimation))

            prediction = torch.stack(cascades_echoes_predictions, dim=1)
            if prediction.shape[-1] != 2:
                prediction = torch.view_as_real(prediction)
            y = fft.fft2(
                utils.complex_mul(prediction.unsqueeze(self.coil_dim), sensitivity_maps.unsqueeze(self.coil_dim - 1)),
                self.fft_centered,
                self.fft_normalization,
                self.spatial_dims,
            )
            recon_prediction = torch.view_as_complex(prediction).clone()

            R2star_maps_init = []
            S0_maps_init = []
            B0_maps_init = []
            phi_maps_init = []
            for batch_idx in range(prediction.shape[0]):
                R2star_map_init, S0_map_init, B0_map_init, phi_map_init = transforms.R2star_B0_S0_phi_mapping(
                    prediction[batch_idx],
                    TEs,
                    mask_brain,
                    torch.ones_like(mask_brain),
                    fully_sampled=True,
                    shift=self.shift_B0_input,
                    fft_centered=self.fft_centered,
                    fft_normalization=self.fft_normalization,
                    spatial_dims=self.spatial_dims,
                )
                R2star_maps_init.append(R2star_map_init.squeeze(0))
                S0_maps_init.append(S0_map_init.squeeze(0))
                B0_maps_init.append(B0_map_init.squeeze(0))
                phi_maps_init.append(phi_map_init.squeeze(0))
            R2star_map_init = torch.stack(R2star_maps_init, dim=0).to(y)
            S0_map_init = torch.stack(S0_maps_init, dim=0).to(y)
            B0_map_init = torch.stack(B0_maps_init, dim=0).to(y)
            phi_map_init = torch.stack(phi_maps_init, dim=0).to(y)

        R2star_map_pred = R2star_map_init / self.gamma[0]
        S0_map_pred = S0_map_init / self.gamma[1]
        B0_map_pred = B0_map_init / self.gamma[2]
        phi_map_pred = phi_map_init / self.gamma[3]

        prediction = None
        for cascade in self.qvn:
            # Forward pass through the cascades
            prediction = cascade(
                y,
                R2star_map_pred,
                S0_map_pred,
                B0_map_pred,
                phi_map_pred,
                TEs,
                sensitivity_maps,
                sampling_mask,
                prediction,
                self.gamma,
            )
            final_prediction = prediction

            R2star_map_pred, S0_map_pred, B0_map_pred, phi_map_pred = (
                prediction[:, 0],
                prediction[:, 1],
                prediction[:, 2],
                prediction[:, 3],
            )
            if R2star_map_pred.shape[-1] == 2:
                R2star_map_pred = torch.view_as_complex(R2star_map_pred)
            if S0_map_pred.shape[-1] == 2:
                S0_map_pred = torch.view_as_complex(S0_map_pred)
            if B0_map_pred.shape[-1] == 2:
                B0_map_pred = torch.view_as_complex(B0_map_pred)
            if phi_map_pred.shape[-1] == 2:
                phi_map_pred = torch.view_as_complex(phi_map_pred)

            prediction = torch.stack(
                [torch.abs(R2star_map_pred), torch.abs(S0_map_pred), torch.abs(B0_map_pred), torch.abs(phi_map_pred)],
                dim=1,
            )

        R2star_map_pred, S0_map_pred, B0_map_pred, phi_map_pred = self.process_intermediate_pred(
            torch.abs(torch.view_as_complex(final_prediction))
        )

        return [
            recon_prediction if self.use_reconstruction_module else torch.empty([]),
            R2star_map_pred,
            S0_map_pred,
            B0_map_pred,
            phi_map_pred,
        ]

    def process_intermediate_pred(self, x):
        """
        Process the intermediate prediction.

        Parameters
        ----------
        x : torch.Tensor
            Prediction of shape [batch_size, n_coils, n_x, n_y, 2].

        Returns
        -------
        torch.Tensor
            Processed prediction of shape [batch_size, n_x, n_y, 2].
        """
        x = self.preprocessor.reverse(x, self.gamma)
        R2star_map_pred, S0_map_pred, B0_map_pred, phi_map_pred = (
            x[:, 0, ...],
            x[:, 1, ...],
            x[:, 2, ...],
            x[:, 3, ...],
        )
        return R2star_map_pred, S0_map_pred, B0_map_pred, phi_map_pred
