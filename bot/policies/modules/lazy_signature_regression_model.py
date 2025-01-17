import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import pytorch_lightning as pl
from typing import Optional, Tuple, Any, Union
from .battery import SeqBatteryEnv
from .transforms import time_aug, unorm, max_abs_norm
import signatory


class LazySigRegModel(pl.LightningModule):
    def __init__(
        self,
        battery: SeqBatteryEnv,  # we only use the batched version of seq battery env
        input_size: int,
        hidden_size: int,
        feature_size: int,
        reg_size: int,
        increase_beta_per_n_epoch: int = 1,
        beta_min: float = 0.5,
        beta_increment: float = 0.1,
        beta_max: float = 5.0,
        sig_depth: int = 3,
        dropout: float = 0.25,
        e_step_iters: int = 5,
        augmenter: Optional[Union[nn.Module, Any]] = None,
    ):
        super().__init__()
        self.save_hyperparameters(ignore=["battery", "augmenter"])
        # compute the number of signature channels (feature numbers)
        self.gc_dim = feature_size + 1  # (time augmentation is the extra feature)
        self.gc_sig_channels = signatory.signature_channels(self.gc_dim, sig_depth)
        self.gc_logsig_channels = signatory.logsignature_channels(
            self.gc_dim, sig_depth
        )
        self.feature_size = feature_size
        self.sig_depth = sig_depth
        # 1D convolutions (kernel size = 1 so no time step blending) to extract global and local features
        # "global" features are reduced to feature_size and summarised with signature map
        # representing "state up to now"
        self.global_conv = signatory.Augment(
            input_size,
            (hidden_size, feature_size),
            1,
            include_time=False,
            include_original=False,
        )
        # local features are reduced to hidden_size and directly used in the prediction network for current time step
        self.local_conv = nn.Sequential(
            nn.Conv1d(input_size, hidden_size, 1),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_size),
            nn.Dropout(dropout),
            nn.Conv1d(hidden_size, hidden_size, 1),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_size),
        )
        # prediction networks
        self.fc = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(self.gc_logsig_channels + hidden_size + 1, reg_size),
            nn.ReLU(),
            nn.Linear(reg_size, reg_size),
            nn.ReLU(),
            nn.Linear(reg_size, 2),
        )

        # self.sig_batch_norm = nn.BatchNorm1d(self.gc_logsig_channels)
        self.battery = battery
        # beta is the "softness" parameter for soft clamp function in battery capacity / charge rate cutoff
        # soft clamping is used to allow gradients to flow through the cutoff points
        self.beta = beta_min
        self.beta_min = beta_min
        self.beta_increment = beta_increment
        self.beta_max = beta_max
        self.increase_beta_per_n_epoch = increase_beta_per_n_epoch

        self.e_step_iters = e_step_iters

        self.augmenter = augmenter
        self.expanding_logsig_map = signatory.LogSignature(depth=sig_depth, stream=True)

    def reset_beta(self):
        self.beta = self.beta_min

    def on_fit_start(self):
        self.battery.to(self.device)

    def on_train_start(self):
        self.reset_beta()

    def e_step(
        self,
        grid_action: torch.Tensor,
        pv_action: torch.Tensor,
        pv: torch.Tensor,
        pr: torch.Tensor,
        peak_indicator: torch.Tensor = None,
    ):
        battery_trace, costs = self.battery.forward(
            grid_action,
            pv_action,
            pv,
            pr,
            beta=None,
            random_initial_state=True,
            is_peak_time_if_taxed=peak_indicator,
        )
        return battery_trace, costs

    def m_step(
        self,
        grid_action: torch.Tensor,
        pv_action: torch.Tensor,
        estimated_battery_state: torch.Tensor,
        pv: torch.Tensor,
        pr: torch.Tensor,
        peak_indicator: torch.Tensor = None,
    ):
        grid_action = F.tanh(grid_action)
        pv_action = F.sigmoid(pv_action)
        battery_trace, costs = self.battery.static_forward(
            estimated_battery_state,
            grid_action,
            pv_action,
            pv,
            pr,
            beta=self.beta,
            is_peak_time_if_taxed=peak_indicator,
        )
        return battery_trace, costs

    def forward(
        self,
        x: torch.Tensor,
        pv: torch.Tensor,
        pr: torch.Tensor,
        peak_ind: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # compute learned augmentation through 1d convolutions
        x_global_feats = self.global_conv(x)  # (batch, seq_len, gc_dim)
        # conv requires (batch, channels, seq_len) format but x is (batch, seq_len, channels)
        x_local_feats = self.local_conv(x.permute(0, 2, 1)).permute(
            0, 2, 1
        )  # (batch, seq_len, hidden_size)

        # augment time ticks to global features
        time_ticks = torch.arange(0, x.size(1) * (1 / 12), 1 / 12).to(x.device)
        x_global_feats = torch.cat(
            [
                time_ticks[None, :, None].expand(x_global_feats.size(0), -1, -1),
                x_global_feats,
            ],
            dim=-1,
        )
        x_expanding_logsig = self.expanding_logsig_map(
            x_global_feats, basepoint=x_global_feats[:, 0, :]
        )  # (batch, seq_len, logsig_dim)  # dummy basepoint is the first time tick
        x_expanding_logsig = max_abs_norm(
            x_expanding_logsig
        )  # (batch, seq_len, logsig_dim)

        # here instead of doing a loop, we try to use an EM-like approach
        # we first play out the actions to obtain the battery trace if we follow the actions
        # then we use the battery trace to compute the costs and gradients
        # then we use the gradients to update the actions
        # then we repeat the process

        # estimate battery trace using blindly proposed actions
        with torch.no_grad():
            battery_trace = self.battery.get_neutral_trace(x.size(0), x.size(1)).to(
                self.device
            )
            for _ in range(self.e_step_iters):
                combined_features = torch.cat(
                    [
                        x_expanding_logsig,  # (batch, seq_len, logsig_dim)
                        x_local_feats,  # (batch, seq_len, hidden_size)
                        battery_trace[:, 1:, None] / self.battery.capacity_kWh,
                    ],
                    dim=-1,
                )  # (batch, seq_len, logsig_dim + hidden_size + 1)
                actions = self.fc(combined_features)  # (batch, seq_len, 2)
                grid_actions, pv_actions = actions[..., 0], actions[..., 1]
                grid_actions = F.tanh(grid_actions)
                pv_actions = F.sigmoid(pv_actions)

                battery_trace, _costs = self.e_step(
                    grid_actions,
                    pv_actions,
                    pv,
                    pr,
                    peak_indicator=peak_ind,
                )
        # estimate actions based on battery_trace
        full_features = torch.cat(
            [
                x_expanding_logsig,  # (batch, seq_len, logsig_dim)
                x_local_feats,  # (batch, seq_len, hidden_size)
                battery_trace[:, 1:, None] / self.battery.capacity_kWh,
            ],
            dim=-1,
        )
        actions = self.fc(full_features)  # (batch, seq_len, 2)
        grid_actions, pv_actions = actions[..., 0], actions[..., 1]
        grid_actions = F.tanh(grid_actions)
        pv_actions = F.sigmoid(pv_actions)
        # estimate costs
        fantasy_battery_trace, costs = self.m_step(
            grid_actions,
            pv_actions,
            battery_trace[:, 1:],
            pv,
            pr,
            peak_indicator=peak_ind,
        )
        return grid_actions, pv_actions, battery_trace, costs

    def step_forward(
        self,
        battery_state: torch.Tensor,
        x_t: torch.Tensor,
        path: Optional[signatory.Path] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        x_global_feats = self.global_conv(x_t)  # (batch, seq_len[1], gc_dim)
        x_local_feats = self.local_conv(x_t.permute(0, 2, 1)).permute(0, 2, 1)

        if path is None:
            current_time_tick = torch.zeros(1).to(x_t.device)
            x_global_feats = torch.cat(
                [
                    current_time_tick[None, :, None].expand(
                        x_global_feats.size(0), -1, -1
                    ),
                    x_global_feats,
                ],
                dim=-1,
            )
            path = signatory.Path(
                x_global_feats, self.sig_depth, basepoint=x_global_feats[:, 0, :]
            )
        else:
            # time augmentation
            current_time_tick = torch.tensor([(path.size(1) - 2) * (1 / 12)]).to(
                x_t.device
            )
            x_global_feats = torch.cat(
                [
                    current_time_tick[None, :, None].expand_as(x_global_feats),
                    x_global_feats,
                ],
                dim=-1,
            )
            # update path with new global features
            path.update(x_global_feats)
        x_current_logsig = max_abs_norm(path.logsignature())  # (batch, logsig_dim)
        x_t = torch.cat(
            [
                x_current_logsig[:, None, :],
                x_local_feats,
                battery_state[:, None, :] / self.battery.capacity_kWh,
            ],
            dim=-1,
        )  # (batch, seq_len[1], logsig_dim + hidden_size + 1)
        z_t = self.fc(x_t[:, 0, :])[:, None, :]

        grid_action, pv_action = z_t[..., 0], z_t[..., 1]
        grid_action = F.tanh(grid_action)
        pv_action = F.sigmoid(pv_action)

        return grid_action, pv_action, path

    def configure_optimizers(self):
        return optim.Adam(self.parameters())

    def training_step(
        self,
        batch: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
        batch_idx: int,
    ):
        grid_actions, pv_actions, fantasy_battery_states, costs = self(*batch)
        loss = costs.sum(1).mean()
        self.log(
            "train_loss",
            loss,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )
        return loss

    def validation_step(
        self,
        batch: Tuple[torch.Tensor, torch.Tensor, torch.Tensor],
        batch_idx: int,
    ):
        grid_actions, pv_actions, fantasy_battery_states, costs = self(*batch)
        loss = costs.sum(1).mean()
        self.log(
            "val_loss",
            loss,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )
        return loss

    def on_train_epoch_end(self) -> None:
        current_epoch = self.current_epoch
        if current_epoch % self.increase_beta_per_n_epoch == 0:
            self.beta = min(self.beta + self.beta_increment, self.beta_max)
        return super().on_train_epoch_end()

    def on_predict_start(self):
        self.beta = None

    def predict_step(
        self, batch: torch.Tensor, batch_idx: int, dataloader_idx: int = 0
    ):
        return self(*batch)

    def on_before_batch_transfer(self, batch: Any, dataloader_idx: int) -> Any:
        if (self.training) & (self.augmenter is not None):
            state, pv_power, price, peak_ind = batch
            state = self.augmenter(state)
            batch = (state, pv_power, price, peak_ind)
        return batch
