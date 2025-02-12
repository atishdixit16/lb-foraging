from collections import namedtuple, defaultdict
from enum import Enum
from itertools import product, combinations
import logging
from typing import Iterable
import operator
from copy import copy, deepcopy
from itertools import combinations

from gym import Env
import gym
from gym.utils import seeding
import numpy as np


class Action(Enum):
    NONE = 0
    NORTH = 1
    SOUTH = 2
    WEST = 3
    EAST = 4
    LOAD = 5


class CellEntity(Enum):
    # entity encodings for grid observations
    OUT_OF_BOUNDS = 0
    EMPTY = 1
    FOOD = 2
    AGENT = 3


class Player:
    def __init__(self):
        self.controller = None
        self.position = None
        self.level = None
        self.field_size = None
        self.score = None
        self.reward = 0
        self.history = None
        self.current_step = None
        self.load_logic = None

    def setup(self, position, level, load_logic, field_size):
        self.history = []
        self.position = position
        self.level = level
        self.field_size = field_size
        self.score = 0
        self.load_logic = self.get_logic_condition(load_logic)

    def get_logic_condition(self,load_logic):
        if load_logic == 'le':
            return operator.le
        if load_logic == 'lt':
            return operator.lt
        if load_logic == 'eq':
            return operator.eq
        
    def get_load_logic_label(self):
        if self.load_logic == operator.le:
            return [1, 1]
        if self.load_logic == operator.lt:
            return [1, 0]
        if self.load_logic == operator.eq:
            return [0, 1]

    def set_controller(self, controller):
        self.controller = controller

    def step(self, obs):
        return self.controller._step(obs)

    @property
    def name(self):
        if self.controller:
            return self.controller.name
        else:
            return "Player"


class ForagingEnv(Env):
    """
    A class that contains rules/actions for the game level-based foraging.
    """

    metadata = {"render.modes": ["human", "rgb_array"]}

    action_set = [Action.NORTH, Action.SOUTH, Action.WEST, Action.EAST, Action.LOAD]
    Observation = namedtuple(
        "Observation",
        ["field", "actions", "players", "game_over", "sight", "current_step"],
    )
    PlayerObservation = namedtuple(
        "PlayerObservation", ["position", "level", "history", "reward", "is_self", "load_logic_label"]
    )  # reward is available only if is_self

    def __init__(
        self,
        players,
        min_player_level,
        max_player_level,
        player_load_logic,
        min_food_level,
        max_food_level,
        field_size,
        max_num_food,
        sight,
        max_episode_steps,
        force_coop,
        normalize_reward=True,
        grid_observation=False,
        penalty=0.0,
    ):
        self.logger = logging.getLogger(__name__)
        self.seed()
        self.players = [Player() for _ in range(players)]

        self.field = np.zeros(field_size, np.int32)

        self.penalty = penalty

        if isinstance(min_food_level, Iterable):
            assert len(min_food_level) == max_num_food, "min_food_level must be a scalar or a list of length max_num_food"
            self.min_food_level = min_food_level
        else:
            self.min_food_level = [min_food_level] * max_num_food
        
        if max_food_level is None:
            self.max_food_level = None
        elif isinstance(max_food_level, Iterable):
            assert len(max_food_level) == max_num_food, "max_food_level must be a scalar or a list of length max_num_food"
            self.max_food_level = max_food_level
        else:
            self.max_food_level = [max_food_level] * max_num_food

        if self.max_food_level is not None:
            # check if min_food_level is less than max_food_level
            for min_food_level, max_food_level in zip(self.min_food_level, self.max_food_level):
                assert min_food_level <= max_food_level, "min_food_level must be less than or equal to max_food_level for each food"

        self.max_num_food = max_num_food
        self._food_spawned = 0.0

        if isinstance(min_player_level, Iterable):
            assert len(min_player_level) == players, "min_player_level must be a scalar or a list of length players"
            self.min_player_level = min_player_level
        else:
            self.min_player_level = [min_player_level] * players

        if isinstance(max_player_level, Iterable):
            assert len(max_player_level) == players, "max_player_level must be a scalar or a list of length players"
            self.max_player_level = max_player_level
        else:
            self.max_player_level = [max_player_level] * players

        if self.max_player_level is not None:
            # check if min_player_level is less than max_player_level for each player
            for i, (min_player_level, max_player_level) in enumerate(zip(self.min_player_level, self.max_player_level)):
                assert min_player_level <= max_player_level, f"min_player_level must be less than or equal to max_player_level for each player but was {min_player_level} > {max_player_level} for player {i}"

        if player_load_logic is None: 
            self.player_load_logic = (['le', 'eq', 'lt']*players)[:players]
        elif isinstance(player_load_logic, Iterable) and not isinstance(player_load_logic, str):
            assert len(player_load_logic) == players, "player_load_logic must be a scalar or a list of length players"
            for logic in player_load_logic:
                assert logic in ('le', 'eq', 'lt'), "invalid input: player_load_logic must be either le (less that or equal), lt (less than) or eq (equal) but recived {0}".format(logic)
            self.player_load_logic = player_load_logic
        else:
            self.player_load_logic = [player_load_logic] * players

        
        self.sight = sight
        self.force_coop = force_coop
        self._game_over = None

        self._rendering_initialized = False
        self._valid_actions = None
        self._max_episode_steps = max_episode_steps

        self._normalize_reward = normalize_reward
        self._grid_observation = grid_observation

        self.action_space = gym.spaces.Tuple(tuple([gym.spaces.Discrete(6)] * len(self.players)))
        self.observation_space = gym.spaces.Tuple(tuple([self._get_observation_space()] * len(self.players)))

        self.viewer = None

        self.n_agents = len(self.players)

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _get_observation_space(self):
        """The Observation Space for each agent.
        - all of the board (board_size^2) with foods
        - player description (x, y, level)*player_count
        """
        player_levels = sorted(self.max_player_level)
        max_food_level = max(self.max_food_level) if self.max_food_level is not None else sum(player_levels[:3])
        if not self._grid_observation:
            field_x = self.field.shape[1]
            field_y = self.field.shape[0]
            # field_size = field_x * field_y

            max_num_food = self.max_num_food

            min_obs = [-1, -1, 0] * max_num_food + [-1, -1, 0, 0, 0] * len(self.players)
            max_obs = [field_x-1, field_y-1, max_food_level] * max_num_food + [
                field_x-1, field_y-1, max(self.max_player_level), 1, 1
            ] * len(self.players)
        else:
            # grid observation space
            grid_shape = (1 + 2 * self.sight, 1 + 2 * self.sight)

            # agents layer: agent levels
            agents_min = np.zeros(grid_shape, dtype=np.float32)
            agents_max = np.ones(grid_shape, dtype=np.float32) * max(self.max_player_level)

            # foods layer: foods level
            foods_min = np.zeros(grid_shape, dtype=np.float32)
            foods_max = np.ones(grid_shape, dtype=np.float32) * max_food_level

            # access layer: i the cell available
            access_min = np.zeros(grid_shape, dtype=np.float32)
            access_max = np.ones(grid_shape, dtype=np.float32)

            # total layer
            min_obs = np.stack([agents_min, foods_min, access_min])
            max_obs = np.stack([agents_max, foods_max, access_max])

        return gym.spaces.Box(np.array(min_obs), np.array(max_obs), dtype=np.float32)

    @classmethod
    def from_obs(cls, obs):
        players = []
        for p in obs.players:
            player = Player()
            player.setup(p.position, p.level, p.load_logic, obs.field.shape)
            player.score = p.score if p.score else 0
            players.append(player)

        env = cls(players, None, None, None, None)
        env.field = np.copy(obs.field)
        env.current_step = obs.current_step
        env.sight = obs.sight
        env._gen_valid_moves()

        return env

    @property
    def field_size(self):
        return self.field.shape

    @property
    def rows(self):
        return self.field_size[0]

    @property
    def cols(self):
        return self.field_size[1]

    @property
    def game_over(self):
        return self._game_over

    def _gen_valid_moves(self):
        self._valid_actions = {
            player: [
                action for action in Action if self._is_valid_action(player, action)
            ]
            for player in self.players
        }

    def neighborhood(self, row, col, distance=1, ignore_diag=False):
        if not ignore_diag:
            return self.field[
                max(row - distance, 0) : min(row + distance + 1, self.rows),
                max(col - distance, 0) : min(col + distance + 1, self.cols),
            ]

        return (
            self.field[
                max(row - distance, 0) : min(row + distance + 1, self.rows), col
            ].sum()
            + self.field[
                row, max(col - distance, 0) : min(col + distance + 1, self.cols)
            ].sum()
        )

    def adjacent_food(self, row, col):
        return (
            self.field[max(row - 1, 0), col]
            + self.field[min(row + 1, self.rows - 1), col]
            + self.field[row, max(col - 1, 0)]
            + self.field[row, min(col + 1, self.cols - 1)]
        )

    def adjacent_food_location(self, row, col):
        if row > 1 and self.field[row - 1, col] > 0:
            return row - 1, col
        elif row < self.rows - 1 and self.field[row + 1, col] > 0:
            return row + 1, col
        elif col > 1 and self.field[row, col - 1] > 0:
            return row, col - 1
        elif col < self.cols - 1 and self.field[row, col + 1] > 0:
            return row, col + 1

    def adjacent_players(self, row, col):
        return [
            player
            for player in self.players
            if abs(player.position[0] - row) == 1
            and player.position[1] == col
            or abs(player.position[1] - col) == 1
            and player.position[0] == row
        ]

    def spawn_food(self, max_num_food, min_levels, max_levels):
        food_count = 0
        attempts = 0
        min_levels = max_levels if self.force_coop else min_levels

        while food_count < max_num_food and attempts < 1000:
            attempts += 1
            row = self.np_random.randint(1, self.rows - 1)
            col = self.np_random.randint(1, self.cols - 1)

            # check if it has neighbors:
            if (
                self.neighborhood(row, col).sum() > 0
                or self.neighborhood(row, col, distance=2, ignore_diag=True) > 0
                or not self._is_empty_location(row, col)
            ):
                continue

            self.field[row, col] = (
                min_levels[food_count]
                if min_levels[food_count] == max_levels[food_count]
                else self.np_random.randint(min_levels[food_count], max_levels[food_count] + 1)
            )
            food_count += 1
        self._food_spawned = self.field.sum()

    def _is_empty_location(self, row, col):
        if self.field[row, col] != 0:
            return False
        for a in self.players:
            if a.position and row == a.position[0] and col == a.position[1]:
                return False

        return True

    def spawn_players(self, min_player_levels, max_player_levels, player_load_logic):
        for player, min_player_level, max_player_level, load_logic in zip(self.players, min_player_levels, max_player_levels, player_load_logic):

            attempts = 0
            player.reward = 0

            while attempts < 1000:
                row = self.np_random.randint(0, self.rows)
                col = self.np_random.randint(0, self.cols)
                if self._is_empty_location(row, col):
                    player.setup(
                        (row, col),
                        self.np_random.randint(min_player_level, max_player_level + 1),
                        load_logic,
                        self.field_size,
                    )
                    break
                attempts += 1

    def _is_valid_action(self, player, action):
        if action == Action.NONE:
            return True
        elif action == Action.NORTH:
            return (
                player.position[0] > 0
                and self.field[player.position[0] - 1, player.position[1]] == 0
            )
        elif action == Action.SOUTH:
            return (
                player.position[0] < self.rows - 1
                and self.field[player.position[0] + 1, player.position[1]] == 0
            )
        elif action == Action.WEST:
            return (
                player.position[1] > 0
                and self.field[player.position[0], player.position[1] - 1] == 0
            )
        elif action == Action.EAST:
            return (
                player.position[1] < self.cols - 1
                and self.field[player.position[0], player.position[1] + 1] == 0
            )
        elif action == Action.LOAD:
            return self.adjacent_food(*player.position) > 0

        self.logger.error("Undefined action {} from {}".format(action, player.name))
        raise ValueError("Undefined action")

    def _transform_to_neighborhood(self, center, sight, position):
        return (
            position[0] - center[0] + min(sight, center[0]),
            position[1] - center[1] + min(sight, center[1]),
        )

    def get_valid_actions(self) -> list:
        return list(product(*[self._valid_actions[player] for player in self.players]))

    def _make_obs(self, player):
        return self.Observation(
            actions=self._valid_actions[player],
            players=[
                self.PlayerObservation(
                    position=self._transform_to_neighborhood(
                        player.position, self.sight, a.position
                    ),
                    level=a.level,
                    is_self=a == player,
                    history=a.history,
                    reward=a.reward if a == player else None,
                    load_logic_label=a.get_load_logic_label(),
                )
                for a in self.players
                if (
                    min(
                        self._transform_to_neighborhood(
                            player.position, self.sight, a.position
                        )
                    )
                    >= 0
                )
                and max(
                    self._transform_to_neighborhood(
                        player.position, self.sight, a.position
                    )
                )
                <= 2 * self.sight
            ],
            # todo also check max?
            field=np.copy(self.neighborhood(*player.position, self.sight)),
            game_over=self.game_over,
            sight=self.sight,
            current_step=self.current_step,
        )

    def _make_gym_obs(self):
        def make_obs_array(observation):
            obs = np.zeros(self.observation_space[0].shape, dtype=np.float32)
            # obs[: observation.field.size] = observation.field.flatten()
            # self player is always first
            seen_players = [p for p in observation.players if p.is_self] + [
                p for p in observation.players if not p.is_self
            ]

            for i in range(self.max_num_food):
                obs[3 * i] = -1
                obs[3 * i + 1] = -1
                obs[3 * i + 2] = 0

            for i, (y, x) in enumerate(zip(*np.nonzero(observation.field))):
                obs[3 * i] = y
                obs[3 * i + 1] = x
                obs[3 * i + 2] = observation.field[y, x]

            for i in range(len(self.players)):
                obs[self.max_num_food * 3 + 5 * i] = -1
                obs[self.max_num_food * 3 + 5 * i + 1] = -1
                obs[self.max_num_food * 3 + 5 * i + 2] = 0
                obs[self.max_num_food * 3 + 5 * i + 3] = 0
                obs[self.max_num_food * 3 + 5 * i + 4] = 0

            for i, p in enumerate(seen_players):
                obs[self.max_num_food * 3 + 5 * i] = p.position[0]
                obs[self.max_num_food * 3 + 5 * i + 1] = p.position[1]
                obs[self.max_num_food * 3 + 5 * i + 2] = p.level
                obs[self.max_num_food * 3 + 5 * i + 3] = p.load_logic_label[0]
                obs[self.max_num_food * 3 + 5 * i + 4] = p.load_logic_label[1]

            return obs

        def make_global_grid_arrays():
            """
            Create global arrays for grid observation space
            """
            grid_shape_x, grid_shape_y = self.field_size
            grid_shape_x += 2 * self.sight
            grid_shape_y += 2 * self.sight
            grid_shape = (grid_shape_x, grid_shape_y)

            agents_layer = np.zeros(grid_shape, dtype=np.float32)
            for player in self.players:
                player_x, player_y = player.position
                agents_layer[player_x + self.sight, player_y + self.sight] = player.level
            
            foods_layer = np.zeros(grid_shape, dtype=np.float32)
            foods_layer[self.sight:-self.sight, self.sight:-self.sight] = self.field.copy()

            access_layer = np.ones(grid_shape, dtype=np.float32)
            # out of bounds not accessible
            access_layer[:self.sight, :] = 0.0
            access_layer[-self.sight:, :] = 0.0
            access_layer[:, :self.sight] = 0.0
            access_layer[:, -self.sight:] = 0.0
            # agent locations are not accessible
            for player in self.players:
                player_x, player_y = player.position
                access_layer[player_x + self.sight, player_y + self.sight] = 0.0
            # food locations are not accessible
            foods_x, foods_y = self.field.nonzero()
            for x, y in zip(foods_x, foods_y):
                access_layer[x + self.sight, y + self.sight] = 0.0
            
            return np.stack([agents_layer, foods_layer, access_layer])

        def get_agent_grid_bounds(agent_x, agent_y):
            return agent_x, agent_x + 2 * self.sight + 1, agent_y, agent_y + 2 * self.sight + 1
        
        def get_player_reward(observation):
            for p in observation.players:
                if p.is_self:
                    return p.reward

        observations = [self._make_obs(player) for player in self.players]
        if self._grid_observation:
            layers = make_global_grid_arrays()
            agents_bounds = [get_agent_grid_bounds(*player.position) for player in self.players]
            nobs = tuple([layers[:, start_x:end_x, start_y:end_y] for start_x, end_x, start_y, end_y in agents_bounds])
        else:
            nobs = tuple([make_obs_array(obs) for obs in observations])
        nreward = [get_player_reward(obs) for obs in observations]
        ndone = [obs.game_over for obs in observations]
        # ninfo = [{'observation': obs} for obs in observations]
        ninfo = {}
        
        # check the space of obs
        for i, obs in  enumerate(nobs):
            assert self.observation_space[i].contains(obs), \
                f"obs space error: obs: {obs}, obs_space: {self.observation_space[i]}"
        
        return nobs, nreward, ndone, ninfo

    def reset(self):
        self.field = np.zeros(self.field_size, np.int32)
        self.spawn_players(self.min_player_level, self.max_player_level, self.player_load_logic)
        player_levels = [player.level for player in self.players]

        self.spawn_food(
            self.max_num_food,
            min_levels=self.min_food_level,
            max_levels=self.max_food_level if self.max_food_level else [max(player_levels)] * self.max_num_food,
        )
        self.current_step = 0
        self._game_over = False
        self._gen_valid_moves()

        nobs, _, _, _ = self._make_gym_obs()
        return nobs
    
    def compute_rewards(self, players, actions):

        for p in players:
            p.reward = 0

        # process the loadings and compute rewards:
        loading_players = set()

        for player, action in zip(players, actions):
            if action == Action.LOAD:
                loading_players.add(player)

        
        while loading_players:
            # find adjacent food
            player = loading_players.pop()
            frow, fcol = self.adjacent_food_location(*player.position)
            food = self.field[frow, fcol]

            adj_players = [ player for player in players
                           if abs(player.position[0] - frow) == 1 and 
                           player.position[1] == fcol or 
                           abs(player.position[1] - fcol) == 1 and 
                           player.position[0] == frow ]

            # choose the adjacent players that are loading and follow same load logic (to get adj_player_level)
            adj_players_logic = [
                p for p in adj_players if (p in loading_players and p.load_logic == player.load_logic ) or p is player
            ]

            adj_player_level = sum([a.level for a in adj_players_logic])


            if not player.load_logic(food,adj_player_level):
                # failed to load
                for a in adj_players_logic:
                    a.reward -= self.penalty
                
                # remove loading players with same logic
                loading_players = loading_players - set(adj_players_logic)
                continue

            # else the food was loaded and each player scores points
            for a in adj_players_logic:
                a.reward = float(a.level * food)
                if self._normalize_reward:
                    a.reward = a.reward / float(
                        adj_player_level * self._food_spawned
                    )  # normalize reward
            
            # remove all adjacent players that do not follow the same load logic
            adj_players_all = [
                p for p in adj_players if p in loading_players or p is player
            ]
            loading_players = loading_players - set(adj_players_all)

        return [player.reward for player in players]
    
    def swap_players(self, swap, actions):
        players_copy = deepcopy(self.players)
        actions_copy = copy(actions)
        actions_copy[swap[0]], actions_copy[swap[1]] = actions_copy[swap[1]], actions_copy[swap[0]] 
        players_copy[swap[0]].position, players_copy[swap[1]].position = players_copy[swap[1]].position, players_copy[swap[0]].position 

        return players_copy, actions_copy

    def reward_mapping_function(self, actions):
        players_copy =  deepcopy(self.players)
        actions = [ Action(a) for a in actions ]

        # check if loading actions are valid
        for i, (player, action) in enumerate(zip(players_copy, actions)):
            if action == Action.LOAD:
                if not self.adjacent_food(*player.position) > 0:
                    actions[i] = Action.NONE

        reward_array = np.zeros((len(players_copy), len(players_copy)))

        # return zero reward when there is no loading
        if Action.LOAD not in actions:
            return reward_array

        # diagonal values of reward_array are default reward values
        r_list = self.compute_rewards(players_copy, actions)
        for i,r in enumerate(r_list):
            reward_array[i,i] = r
        
        # combination of agents' trajecory sharing for reward mapping
        swap_combs = combinations(list(range(len(self.players))), 2)
        for swap in swap_combs:
            swapped_players, swapped_actions = self.swap_players(swap, actions)
            r_list = self.compute_rewards(swapped_players, swapped_actions)
            reward_array[swap[0], swap[1] ] = r_list[swap[0]]
            reward_array[swap[1], swap[0] ] = r_list[swap[1]]

        return reward_array

    def step(self, actions):
        self.current_step += 1

        for p in self.players:
            p.reward = 0

        actions = [
            Action(a) if Action(a) in self._valid_actions[p] else Action.NONE
            for p, a in zip(self.players, actions)
        ]

        # check if actions are valid
        for i, (player, action) in enumerate(zip(self.players, actions)):
            if action not in self._valid_actions[player]:
                self.logger.info(
                    "{}{} attempted invalid action {}.".format(
                        player.name, player.position, action
                    )
                )
                actions[i] = Action.NONE

        loading_players = set()

        # move players
        # if two or more players try to move to the same location they all fail
        collisions = defaultdict(list)

        # so check for collisions
        for player, action in zip(self.players, actions):
            if action == Action.NONE:
                collisions[player.position].append(player)
            elif action == Action.NORTH:
                collisions[(player.position[0] - 1, player.position[1])].append(player)
            elif action == Action.SOUTH:
                collisions[(player.position[0] + 1, player.position[1])].append(player)
            elif action == Action.WEST:
                collisions[(player.position[0], player.position[1] - 1)].append(player)
            elif action == Action.EAST:
                collisions[(player.position[0], player.position[1] + 1)].append(player)
            elif action == Action.LOAD:
                collisions[player.position].append(player)
                loading_players.add(player)

        # and do movements for non colliding players

        for k, v in collisions.items():
            if len(v) > 1:  # make sure no more than an player will arrive at location
                continue
            v[0].position = k

        # finally process the loadings:
        while loading_players:
            # find adjacent food
            player = loading_players.pop()
            frow, fcol = self.adjacent_food_location(*player.position)
            food = self.field[frow, fcol]

            adj_players = self.adjacent_players(frow, fcol)

            # choose the adjacent players that are loading and follow same load logic (to get adj_player_level)
            adj_players_logic = [
                p for p in adj_players if (p in loading_players and p.load_logic == player.load_logic ) or p is player
            ]

            adj_player_level = sum([a.level for a in adj_players_logic])


            if not player.load_logic(food,adj_player_level):
                # failed to load
                for a in adj_players_logic:
                    a.reward -= self.penalty
                
                # remove loading players with same logic
                loading_players = loading_players - set(adj_players_logic)
                continue

            # else the food was loaded and each player scores points
            for a in adj_players_logic:
                a.reward = float(a.level * food)
                if self._normalize_reward:
                    a.reward = a.reward / float(
                        adj_player_level * self._food_spawned
                    )  # normalize reward
            # and the food is removed
            self.field[frow, fcol] = 0
            
            # remove all adjacent players that do not follow the same load logic
            adj_players_all = [
                p for p in adj_players if p in loading_players or p is player
            ]
            loading_players = loading_players - set(adj_players_all)

        self._game_over = (
            self.field.sum() == 0 or self._max_episode_steps <= self.current_step
        )
        self._gen_valid_moves()

        for p in self.players:
            p.score += p.reward

        return self._make_gym_obs()

    def _init_render(self):
        from .rendering import Viewer

        self.viewer = Viewer((self.rows, self.cols))
        self._rendering_initialized = True

    def render(self, mode="human"):
        if not self._rendering_initialized:
            self._init_render()

        return self.viewer.render(self, return_rgb_array=mode == "rgb_array")

    def close(self):
        if self.viewer:
            self.viewer.close()