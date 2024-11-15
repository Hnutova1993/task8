# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 Syeam Bin Abdullah

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


import argparse
import asyncio
import copy
import os
import threading
from traceback import print_exception

import bittensor as bt
import torch
from dotenv import load_dotenv
from web3 import Web3

from sturdy.base.neuron import BaseNeuron
from sturdy.constants import QUERY_RATE
from sturdy.mock import MockDendrite
from sturdy.utils.config import add_validator_args
from sturdy.utils.wandb import init_wandb_validator, reinit_wandb, should_reinit_wandb


class BaseValidatorNeuron(BaseNeuron):
    """
    Base class for Bittensor validators. Your validator should inherit from this class.
    """

    neuron_type: str = "ValidatorNeuron"

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser) -> None:
        super().add_args(parser)
        add_validator_args(cls, parser)

    def __init__(self, config=None) -> None:
        super().__init__(config=config)
        load_dotenv()

        # set last query block to be 0
        self.last_query_block = 0

        # init wandb
        self.wandb_run_log_count = 0
        if not self.config.wandb.off:
            bt.logging.debug("loading wandb")
            init_wandb_validator(self=self)

        # Save a copy of the hotkeys to local memory.
        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

        # set web3 provider url
        w3_provider_url = os.environ.get("WEB3_PROVIDER_URL")
        if w3_provider_url is None:
            raise ValueError("You must provide a valid web3 provider url as an organic validator!")

        self.w3 = Web3(Web3.HTTPProvider(w3_provider_url))

        # Dendrite lets us send messages to other nodes (axons) in the network.
        if self.config.mock:
            self.dendrite = MockDendrite(wallet=self.wallet)
        else:
            self.dendrite = bt.dendrite(wallet=self.wallet)

        bt.logging.info(f"Dendrite: {self.dendrite}")

        # Set up initial scoring weights for validation
        bt.logging.info("Building validation weights.")
        self.scores = torch.zeros(self.metagraph.n, dtype=torch.float32, device=self.device)
        self.similarity_penalties = {}
        self.sorted_apys = {}
        self.sorted_axon_times = {}

        # Init sync with the network. Updates the metagraph.
        self.sync()

        # Serve axon to enable external connections.
        if not self.config.neuron.axon_off:
            self.serve_axon()
        else:
            bt.logging.warning("axon off, not serving ip to chain.")

        # Create asyncio event loop to manage async tasks.
        self.loop = asyncio.get_event_loop()

        # Instantiate runners
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: threading.Thread = None
        self.lock = asyncio.Lock()

    def serve_axon(self) -> None:
        """Serve axon to enable external connections."""

        bt.logging.info("serving ip to chain...")
        try:
            self.axon = bt.axon(wallet=self.wallet, config=self.config)

            try:
                self.subtensor.serve_axon(
                    netuid=self.config.netuid,
                    axon=self.axon,
                )
                bt.logging.info(
                    f"Running validator {self.axon} on network: {self.config.subtensor.chain_endpoint} with netuid: \
                        {self.config.netuid}"
                )
            except Exception as e:
                bt.logging.error(f"Failed to serve Axon with exception: {e}")

        except Exception as e:
            bt.logging.error(f"Failed to create Axon initialize with exception: {e}")

    async def concurrent_forward(self) -> None:
        coroutines = [self.forward() for _ in range(self.config.neuron.num_concurrent_forwards)]
        await asyncio.gather(*coroutines)

    def run(self) -> None:
        """
        Initiates and manages the main loop for the miner on the Bittensor network. The main loop handles graceful shutdown on
        keyboard interrupts and logs unforeseen errors.

        This function performs the following primary tasks:
        1. Check for registration on the Bittensor network.
        2. Continuously forwards queries to the miners on the network, rewarding their responses and updating the scores
        accordingly.
        3. Periodically resynchronizes with the chain; updating the metagraph with the latest network state and setting
        weights.

        The essence of the validator's operations is in the forward function, which is called every step. The forward function
        is responsible for querying the network and scoring the responses.

        Note:
            - The function leverages the global configurations set during the initialization of the miner.
            - The miner's axon serves as its interface to the Bittensor network, handling incoming and outgoing requests.

        Raises:
            KeyboardInterrupt: If the miner is stopped by a manual interruption.
            Exception: For unforeseen errors during the miner's operation, which are logged for diagnosis.
        """

        # Check that validator is registered on the network.
        self.sync()

        bt.logging.info(f"Validator starting at block: {self.block}")

        # This loop maintains the validator's operations until intentionally stopped.
        try:
            while True:
                # Run multiple forwards concurrently - runs every 2 blocks
                current_block = self.subtensor.block
                if current_block - self.last_query_block > QUERY_RATE:
                    bt.logging.info(f"step({self.step}) block({self.block})")

                    if self.config.organic:
                        future = asyncio.run_coroutine_threadsafe(self.concurrent_forward(), self.loop)
                        future.result()  # Wait for the coroutine to complete
                    else:
                        self.loop.run_until_complete(self.concurrent_forward())

                    self.last_query_block = current_block
                    # Sync metagraph and potentially set weights.
                    self.sync()

                    if not self.config.wandb.off:
                        bt.logging.debug("Logging info to wandb")
                        try:
                            metrics_to_log = {
                                f"miner_scores/score_uid_{uid}": float(score) for uid, score in enumerate(self.scores)
                            }
                            other_metrics = {
                                "block": self.block,
                                "validator_run_step": self.step,
                            }
                            sim_penalties = {
                                f"similarity_penalties/uid_{uid}": score for uid, score in self.similarity_penalties.items()
                            }
                            apys = {f"apys/uid_{uid}": apy for uid, apy in self.sorted_apys.items()}
                            axon_times = {
                                f"axon_times/uid_{uid}": axon_time for uid, axon_time in self.sorted_axon_times.items()
                            }
                            metrics_to_log.update(other_metrics)
                            metrics_to_log.update(sim_penalties)
                            metrics_to_log.update(apys)
                            metrics_to_log.update(axon_times)
                            self.wandb.log(metrics_to_log, step=self.block)
                            self.wandb_run_log_count += 1
                            bt.logging.info(
                                f"wandb log count: {self.wandb_run_log_count} | \
                                until reinit: {self.config.wandb.run_log_limit - self.wandb_run_log_count}"
                            )
                        except Exception as e:
                            bt.logging.error("Failed to log info into wandb!")
                            bt.logging.error(e)

                        # rollover to new wandb run if needed:
                        if should_reinit_wandb(self):
                            try:
                                reinit_wandb(self)
                                self.wandb_run_log_count = 0
                            except Exception as e:
                                bt.logging.error("Failed reinit wandb run!")
                                bt.logging.error(e)

                # Check if we should exit.
                if self.should_exit:
                    break

                self.step += 1

        # If someone intentionally stops the validator, it'll safely terminate operations.
        except KeyboardInterrupt:
            self.axon.stop()
            bt.logging.success("Validator killed by keyboard interrupt.")
            exit()

        # In case of unforeseen errors, the validator will log the error and continue operations.
        except Exception as err:
            bt.logging.error("Error during validation", str(err))
            bt.logging.debug(print_exception(type(err), err, err.__traceback__))

    async def run_concurrent_forward(self) -> None:
        try:
            await self.concurrent_forward()
        except Exception as e:
            bt.logging.error(f"Error in concurrent_forward: {e}")

    def run_in_background_thread(self) -> None:
        """
        Starts the validator's operations in a background thread upon entering the context.
        This method facilitates the use of the validator in a 'with' statement.
        """
        if not self.is_running:
            bt.logging.debug("Starting validator in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            bt.logging.debug("Started")

    def stop_run_thread(self) -> None:
        """
        Stops the validator's operations that are running in the background thread.
        """
        if self.is_running:
            bt.logging.debug("Stopping validator in background thread.")
            self.should_exit = True
            self.thread.join(5)
            self.is_running = False
            bt.logging.debug("Stopped")

    def __enter__(self) -> "BaseValidatorNeuron":
        self.run_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """
        Stops the validator's background operations upon exiting the context.
        This method facilitates the use of the validator in a 'with' statement.

        Args:
            exc_type: The type of the exception that caused the context to be exited.
                      None if the context was exited without an exception.
            exc_value: The instance of the exception that caused the context to be exited.
                       None if the context was exited without an exception.
            traceback: A traceback object encoding the stack trace.
                       None if the context was exited without an exception.
        """
        if self.is_running:
            bt.logging.debug("Stopping validator in background thread.")
            self.should_exit = True
            self.thread.join(5)
            self.is_running = False
            bt.logging.debug("Stopped")

            if self.wandb is not None:
                bt.logging.debug("closing wandb connection")
                self.wandb.finish()
                bt.logging.debug("closed wandb connection")
            bt.logging.success("Validator killed")

    def set_weights(self) -> None:
        """
        Sets the validator weights to the metagraph hotkeys based on the scores it has received from the miners. The weights
        determine the trust and incentive level the validator assigns to miner nodes on the network.
        """

        # Check if self.scores contains any NaN values and log a warning if it does.
        if torch.isnan(self.scores).any():
            bt.logging.warning(
                "Scores contain NaN values. This may be due to a lack of responses from miners, or a bug in your reward \
                functions."
            )

        # Calculate the average reward for each uid across non-zero values.
        # Replace any NaN values with 0.
        raw_weights = torch.nn.functional.normalize(self.scores, p=1, dim=0)

        bt.logging.debug("raw_weights", raw_weights)
        bt.logging.debug("raw_weight_uids", self.metagraph.uids.to("cpu"))
        # Process the raw weights to final_weights via subtensor limitations.
        (
            processed_weight_uids,
            processed_weights,
        ) = bt.utils.weight_utils.process_weights_for_netuid(
            uids=self.metagraph.uids.to("cpu"),
            weights=raw_weights.to("cpu"),
            netuid=self.config.netuid,
            subtensor=self.subtensor,
            metagraph=self.metagraph,
        )
        bt.logging.debug("processed_weights", processed_weights)
        bt.logging.debug("processed_weight_uids", processed_weight_uids)

        # Convert to uint16 weights and uids.
        (
            uint_uids,
            uint_weights,
        ) = bt.utils.weight_utils.convert_weights_and_uids_for_emit(uids=processed_weight_uids, weights=processed_weights)
        bt.logging.debug("uint_weights", uint_weights)
        bt.logging.debug("uint_uids", uint_uids)

        # Set the weights on chain via our subtensor connection.
        result, msg = self.subtensor.set_weights(
            wallet=self.wallet,
            netuid=self.config.netuid,
            uids=uint_uids,
            weights=uint_weights,
            wait_for_finalization=False,
            wait_for_inclusion=False,
            version_key=self.spec_version,
        )
        if result is True:
            bt.logging.info("set_weights on chain successfully!")
        else:
            bt.logging.error("set_weights failed", msg)

    def resync_metagraph(self) -> None:
        """Resyncs the metagraph and updates the hotkeys and moving averages based on the new metagraph."""
        bt.logging.info("resync_metagraph()")

        # Copies state of metagraph before syncing.
        previous_metagraph = copy.deepcopy(self.metagraph)

        # Sync the metagraph.
        self.metagraph.sync(subtensor=self.subtensor)

        # Check if the metagraph axon info has changed.
        if previous_metagraph.axons == self.metagraph.axons:
            return

        bt.logging.info("Metagraph updated, re-syncing hotkeys, dendrite pool and moving averages")
        # Zero out all hotkeys that have been replaced.
        for uid, hotkey in enumerate(self.hotkeys):
            if hotkey != self.metagraph.hotkeys[uid]:
                self.scores[uid] = 0  # hotkey has been replaced

        # Check to see if the metagraph has changed size.
        # If so, we need to add new hotkeys and moving averages.
        if len(self.hotkeys) < len(self.metagraph.hotkeys):
            # Update the size of the moving average scores.
            new_moving_average = torch.zeros((self.metagraph.n)).to(self.device)
            min_len = min(len(self.hotkeys), len(self.scores))
            new_moving_average[:min_len] = self.scores[:min_len]
            self.scores = new_moving_average

        # Update the hotkeys.
        self.hotkeys = copy.deepcopy(self.metagraph.hotkeys)

    def update_scores(self, rewards: torch.Tensor, uids: list[int]) -> None:
        """Performs exponential moving average on the scores based on the rewards received from the miners."""

        # Check if rewards contains NaN values.
        if torch.isnan(rewards).any():
            bt.logging.warning(f"NaN values detected in rewards: {rewards}")
            # Replace any NaN values in rewards with 0.
            rewards = torch.nan_to_num(rewards, 0)

        # Check if `uids` is already a tensor and clone it to avoid the warning.
        uids_tensor = uids.clone().detach() if isinstance(uids, torch.Tensor) else torch.tensor(uids).to(self.device)

        # Compute forward pass rewards, assumes uids are mutually exclusive.
        # shape: [ metagraph.n ]
        scattered_rewards: torch.Tensor = self.scores.scatter(0, uids_tensor, rewards).to(self.device)
        bt.logging.debug(f"Scattered rewards: {rewards}")

        # Update scores with rewards produced by this step.
        # shape: [ metagraph.n ]
        alpha: float = self.config.neuron.moving_average_alpha
        self.scores: torch.Tensor = alpha * scattered_rewards + (1 - alpha) * self.scores.to(self.device)
        bt.logging.debug(f"Updated moving avg scores: {self.scores}")

    def save_state(self) -> None:
        """Saves the state of the validator to a file."""
        bt.logging.info("Saving validator state.")

        # Save the state of the validator to file.
        torch.save(
            {
                "step": self.step,
                "scores": self.scores,
                "hotkeys": self.hotkeys,
                "last_query_block": self.last_query_block
            },
            self.config.neuron.full_path + "/state.pt",
        )

    def load_state(self) -> None:
        """Loads the state of the validator from a file."""
        bt.logging.info("Loading validator state.")

        # Load the state of the validator from file.
        state = torch.load(self.config.neuron.full_path + "/state.pt")
        self.step = state["step"]
        self.scores = state["scores"]
        self.hotkeys = state["hotkeys"]
        self.last_query_block = state["last_query_block"]