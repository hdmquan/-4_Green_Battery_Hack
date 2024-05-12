import pandas as pd
from policy import Policy
import torch

from pathlib import Path

here = Path(__file__).parent
root = here.parent

from .modules.stateful_rnn_model import StatefulRNNModel, BatteryEnv
from .modules.segment_dataset import (
    PriceSolarTransformer,
    active_columns,
    drop_no_solar,
)


class Calibration(Policy):
    def __init__(self):
        super().__init__()
        battery = BatteryEnv(13, 5, 7.5)
        self.model = StatefulRNNModel.load_from_checkpoint(
            here / "mx7bx8-i59-val_loss=-158.274.ckpt",
            battery=battery,
            map_location="cpu",
        )
        self.historical_states = []
        training_data = pd.read_csv(root / "data/training_data.csv")
        training_data = drop_no_solar(training_data)
        self.preprocessor = PriceSolarTransformer(
            include_pos_code=True,
            include_peak_indicator=True,
            include_price_augmentations=True,
            interpolate_missing=True,
            price_transform_patch=False,
        ).fit(training_data)
        self.means = pd.read_csv(root / "data/training_means.csv")[active_columns].iloc[0]
        self.h = self.model.get_initial_h(1)

    def act(self, external_state, internal_state):
        # add fields with default value to external_state DataFrame if key is missing
        current = pd.DataFrame(columns=active_columns)
        # fill current with values from external_state if they exist
        for key in active_columns:
            if key in external_state.keys():
                current.loc[0, key] = external_state[key]
            else:
                current.loc[0, key] = self.means[key]
        current = current.iloc[0]

        if len(self.historical_states) > 0:
            last = self.historical_states[-1]
            current.fillna(last, inplace=True)
        else:
            current.fillna(self.means, inplace=True)
        insert = pd.DataFrame([current])
        insert["timestamp"] = pd.to_datetime(insert["timestamp"])
        self.historical_states.append(current)

        state, pv_power, price, peak_indicator = self.preprocessor.transform(insert)
        state = torch.tensor(state).float()[None, ...]
        self.model.eval()
        battery_state = torch.tensor(internal_state["battery_soc"]).float()[None, None]
        with torch.no_grad():
            grid_action, pv_action, battery_state, self.h = self.model.step_forward(
                battery_state, state, self.h
            )
        cr = internal_state["max_charge_rate"]
        pv_charge = pv_action[0, 0] * external_state["pv_power"]
        grid_charge = grid_action[0, 0] * cr
        return pv_charge.cpu().numpy().item(), grid_charge.cpu().numpy().item()

    def load_historical(self, external_states: pd.DataFrame):
        pass