from logging import Logger
import os
from traceback import print_exc
from typing import Optional

from rlbot import flat
from rlbot.interface import SocketRelay
from rlbot.managers import Renderer
from rlbot.utils.logging import DEFAULT_LOGGER, get_logger


class Hivemind:
    """
    A convenience class for building a hivemind bot on top of.
    """

    _logger = DEFAULT_LOGGER
    loggers: list[Logger] = []

    team: int = -1
    indices: list[int] = []
    names: list[str] = []
    spawn_ids: list[int] = []

    match_settings = flat.MatchSettings()
    field_info = flat.FieldInfo()
    ball_prediction = flat.BallPrediction()

    _initialized_bot = False
    _has_match_settings = False
    _has_field_info = False
    _has_player_mapping = False

    _latest_packet: Optional[flat.GameTickPacket] = None
    _latest_prediction = flat.BallPrediction()

    def __init__(self):
        group_id = os.environ.get("RLBOT_GROUP_ID")

        if group_id is None:
            self._logger.critical("RLBOT_GROUP_ID environment variable is not set")
            exit(1)

        self._game_interface = SocketRelay(group_id, logger=self._logger)
        self._game_interface.match_settings_handlers.append(self._handle_match_settings)
        self._game_interface.field_info_handlers.append(self._handle_field_info)
        self._game_interface.match_communication_handlers.append(
            self._handle_match_communication
        )
        self._game_interface.ball_prediction_handlers.append(
            self._handle_ball_prediction
        )
        self._game_interface.packet_handlers.append(self._handle_packet)

        self.renderer = Renderer(self._game_interface)

    def _initialize(self):
        # search match settings for our spawn ids
        for player in self.match_settings.player_configurations:
            if player.spawn_id in self.spawn_ids:
                self.names.append(player.name)
                self.loggers.append(get_logger(player.name))

        try:
            self.initialize()
        except Exception as e:
            self._logger.critical(
                "Hivemind %s failed to initialize due the following error: %s",
                "Unknown" if len(self.names) == 0 else self.names[0],
                e,
            )
            print_exc()
            exit()

        self._initialized_bot = True
        self._game_interface.send_init_complete()

    def _handle_match_settings(self, match_settings: flat.MatchSettings):
        self.match_settings = match_settings
        self._has_match_settings = True

        if (
            not self._initialized_bot
            and self._has_field_info
            and self._has_player_mapping
        ):
            self._initialize()

    def _handle_field_info(self, field_info: flat.FieldInfo):
        self.field_info = field_info
        self._has_field_info = True

        if (
            not self._initialized_bot
            and self._has_match_settings
            and self._has_player_mapping
        ):
            self._initialize()

    def _handle_player_mappings(self, player_mappings: flat.TeamControllables):
        self.team = player_mappings.team
        for controllable in player_mappings.controllables:
            self.spawn_ids.append(controllable.spawn_id)
            self.indices.append(controllable.index)

        self._has_player_mapping = True

        if (
            not self._initialized_bot
            and self._has_match_settings
            and self._has_field_info
        ):
            self._initialize()

    def _handle_ball_prediction(self, ball_prediction: flat.BallPrediction):
        self._latest_prediction = ball_prediction

    def _handle_packet(self, packet: flat.GameTickPacket):
        self._latest_packet = packet

    def _packet_processor(self, packet: flat.GameTickPacket):
        if len(packet.players) <= self.indices[-1]:
            return

        self.ball_prediction = self._latest_prediction

        try:
            controller = self.get_outputs(packet)
        except Exception as e:
            self._logger.error(
                "Hivemind (with %s) returned an error to RLBot: %s", self.names, e
            )
            print_exc()
            return

        for index, controller in controller.items():
            player_input = flat.PlayerInput(index, controller)
            self._game_interface.send_player_input(player_input)

    def run(
        self,
        wants_match_communications: bool = True,
        wants_ball_predictions: bool = True,
    ):
        rlbot_server_port = int(os.environ.get("RLBOT_SERVER_PORT", 23234))

        try:
            self._game_interface.connect(
                wants_match_communications,
                wants_ball_predictions,
                rlbot_server_port=rlbot_server_port,
            )

            # see bot.py for an explanation of this loop
            while True:
                try:
                    self._game_interface.handle_incoming_messages(True)
                    break
                except BlockingIOError:
                    pass

                if self._latest_packet is None:
                    self._game_interface.socket.setblocking(True)
                    continue

                self._packet_processor(self._latest_packet)
                self._latest_packet = None
        finally:
            self.retire()
            del self._game_interface

    def get_match_settings(self) -> flat.MatchSettings:
        """
        Contains info about what map you're on, mutators, etc.
        """
        return self.match_settings

    def get_field_info(self) -> flat.FieldInfo:
        """
        Contains info about the map, such as the locations of boost pads and goals.
        """
        return self.field_info

    def get_ball_prediction(self) -> flat.BallPrediction:
        """
        A simulated prediction of the ball's path with only the field geometry.
        """
        return self.ball_prediction

    def _handle_match_communication(self, match_comm: flat.MatchComm):
        self.handle_match_communication(
            match_comm.index,
            match_comm.team,
            match_comm.content,
            match_comm.display,
            match_comm.team_only,
        )

    def handle_match_communication(
        self,
        index: int,
        team: int,
        content: bytes,
        display: Optional[str],
        team_only: bool,
    ):
        """
        Called when a match communication is received.
        """

    def send_match_comm(
        self,
        index: int,
        content: bytes,
        display: Optional[str] = None,
        team_only: bool = False,
    ):
        """
        Emits a match communication

        - `content`: The other content of the communication containing arbirtrary data.
        - `display`: The message to be displayed in the game, or None to skip displaying a message.
        - `team_only`: If True, only your team will receive the communication.
        """
        self._game_interface.send_match_comm(
            flat.MatchComm(
                index,
                self.team,
                team_only,
                display,
                content,
            )
        )

    def set_game_state(
        self,
        balls: dict[int, flat.DesiredBallState] = {},
        cars: dict[int, flat.DesiredCarState] = {},
        game_info: Optional[flat.DesiredGameInfoState] = None,
        commands: list[flat.ConsoleCommand] = [],
    ):
        """
        Sets the game to the desired state.
        """

        game_state = flat.DesiredGameState(
            game_info_state=game_info, console_commands=commands
        )

        # convert the dictionaries to lists by
        # filling in the blanks with empty states

        if balls:
            max_entry = max(balls.keys())
            game_state.ball_states = [
                balls.get(i, flat.DesiredBallState()) for i in range(max_entry + 1)
            ]

        if cars:
            max_entry = max(cars.keys())
            game_state.car_states = [
                cars.get(i, flat.DesiredCarState()) for i in range(max_entry + 1)
            ]

        self._game_interface.send_game_state(game_state)

    def set_loadout(self, loadout: flat.PlayerLoadout, spawn_id: int):
        """
        Sets the loadout of a bot.

        For use as a loadout generator, call inside of `initialize`.
        Will be ignored if called outside of `initialize` when state setting is disabled.
        """
        self._game_interface.send_set_loadout(flat.SetLoadout(spawn_id, loadout))

    def initialize(self):
        """
        Called for all heaver initialization that needs to happen.
        Field info and match settings are fully loaded at this point, and won't return garbage data.

        NOTE: `self.index` is not set at this point, and should not be used. `self.team` and `self.name` _are_ set with correct information.
        """

    def retire(self):
        """Called after the game ends"""

    def get_outputs(
        self, packet: flat.GameTickPacket
    ) -> dict[int, flat.ControllerState]:
        """
        Where all the logic of your bot gets its input and returns its output.
        """
        raise NotImplementedError
