import functools
import statistics
import numpy as np
import wandb
import collections

from gym.spaces import MultiDiscrete, Box
from pettingzoo import AECEnv
from pettingzoo.utils import wrappers, agent_selector

from drone import Drone, OtherDrone
from packet import Packet
from zone import Zone
from event import TimeMatrix

K = 200


def env(input_c, result_buffer=None):
    '''
    The env function often wraps the environment in wrappers by default.
    You can find full documentation for these methods
    elsewhere in the developer documentation.
    '''
    env = raw_env(input_c, result_buffer)
    # This wrapper is only for environments which print results to the terminal
    # env = wrappers.CaptureStdoutWrapper(env)
    # this wrapper helps error handling for discrete action spaces
    # env = wrappers.AssertOutOfBoundsWrapper(env)
    # Provides a wide vareity of helpful user errors
    # Strongly recommended
    env = wrappers.OrderEnforcingWrapper(env)

    return env


class raw_env(AECEnv):

    metadata = {'render.modes': ['human'], "name": "rps_v2"}

    def __init__(self, input_c, result_buffer=None):
        '''
        The init method takes in environment arguments and should define the following attributes:
        - possible_agents
        - action_spaces
        - observation_spaces

        These attributes should not be changed after initialization.
        '''

        if result_buffer is not None:
            self.save_res = True
            self.res_buffer = result_buffer
            print("found a result buffer!")
        else:
            self.save_res = False

        self.number_of_uavs = input_c.n
        self.processing_rate = input_c.processing_rate
        self.offloading_rate = input_c.offloading_rate
        self.max_time = input_c.max_time

        self.lambdas = [input_c.lmbda_l, input_c.lmbda_h]
        self.prob_trans = input_c.prob_trans

        self.alg = input_c.alg

        self.feature_size = (4 * self.number_of_uavs) + 5  # ProcessingQueue, OffloadingQueue, TrafficPattern, OffProbability + ProcessingRate + single agent
        self.t = 0
        self.tot_reward = 0
        self.obs_max_timer = input_c.obs_max_timer
        self.steps = 0
        self.max_number_of_cpus = 2
        self.delay_weight = 1
        self.consumption_weight = 20 / self.max_number_of_cpus
        if self.alg == "fcto" or self.alg == "woto":
            print("found " + self.alg + " algorithm")
            self.drones = [OtherDrone(self.processing_rate, self.offloading_rate, self.alg) for _ in
                           range(self.number_of_uavs)]
            for drone in self.drones:
                drone.set_drones(self.drones)
        else:
            self.drones = [Drone(i, self.processing_rate, self.offloading_rate) for i in range(self.number_of_uavs)]

        self.zones = [Zone(i, self.lambdas[0], self.lambdas[1], i) for i in range(self.number_of_uavs)]
        self.time_matrix = TimeMatrix(self.number_of_uavs, self.prob_trans, self.lambdas)

        self.possible_agents = ["uav" + str(r) for r in range(self.number_of_uavs)]
        self.agent_name_mapping = dict(zip(self.possible_agents, list(range(len(self.possible_agents)))))
        self._action_spaces = {agent: MultiDiscrete([self.max_number_of_cpus, self.number_of_uavs]) for agent in self.possible_agents}
        self._observation_spaces = {agent: Box(low=0, high=1, shape=(self.feature_size,), dtype=np.float32)
                                    for agent in self.possible_agents}
        # tot, proc, ol
        self.avg_tot_delay = [0, 0, 0]
        self.counter_avg_td = [0, 0, 0]
        # computing pdf
        self.arr_delay = [0]
        # counts how many times each zone does a complete low->high->low cycle
        self.count_cycle_zone = np.zeros(self.number_of_uavs)
        # computes mean delay
        self.delay = []
        # computes epoch delay
        self.current_delay = []
        # computes mean offloading probabilities
        self.offloading_probabilities = []
        # computes mean processing rates
        self.processing_rates = []
        # computes mean delay reward
        self.mean_delay_reward = []
        # computes mean consumption reward
        self.mean_consumption_reward = []
        # computes total mean reward
        self.mean_reward = []
        # insert jobs arrived in previous epoch
        self.jobs_to_schedule = []

        # normalize observation between 0 and 1
        self.max_observed_queue = 1
        self.max_observed_queue_ol = 1
        self.max_observed_job_counter = 1

    # this cache ensures that same space object is returned for the same agent
    # allows action space seeding to work as expected

    @functools.lru_cache(maxsize=None)
    def observation_space(self, agent):
        # Gym spaces are defined and documented here: https://gym.openai.com/docs/#spaces
        return Box(low=0, high=1, shape=(self.feature_size,), dtype=np.float32)

    @functools.lru_cache(maxsize=None)
    def action_space(self, agent):
        # action[0] -> number of cpus to keep on (+ 1)
        # action[1] -> incoming jobs destination
        return MultiDiscrete([self.max_number_of_cpus, self.number_of_uavs])

    def render(self, mode="human"):
        pass

    def close(self):
        '''
        Close should release any graphical displays, subprocesses, network connections
        or any other environment data which should not be kept around after the
        user is no longer using the environment.
        '''
        pass

    def reset(self):

        if self.alg == "fcto" or self.alg == "woto":
            self.drones = [OtherDrone(self.processing_rate, self.offloading_rate, self.alg) for _ in
                           range(self.number_of_uavs)]
            for drone in self.drones:
                drone.set_drones(self.drones)
        else:
            self.drones = [Drone(self.processing_rate, self.offloading_rate) for _ in range(self.number_of_uavs)]
        self.zones = [Zone(i, self.lambdas[0], self.lambdas[1], i) for i in range(self.number_of_uavs)]
        self.time_matrix = TimeMatrix(self.number_of_uavs, self.prob_trans, self.lambdas)

        # reset metrics
        self.avg_tot_delay = [0, 0, 0]
        self.counter_avg_td = [0, 0, 0]
        self.arr_delay = [0]
        self.count_cycle_zone = np.zeros(self.number_of_uavs)
        self.delay = []
        self.current_delay = []
        self.offloading_probabilities = []
        self.processing_rates = []
        self.mean_delay_reward = []
        self.mean_consumption_reward = []
        self.mean_reward = []
        self.jobs_to_schedule = []
        self.agents = self.possible_agents[:]
        self.t = 0
        self.steps = 0
        self.tot_reward = 0
        observations = {agent: self.get_obs(aidx) for aidx, agent in enumerate(self.agents)}
        # new
        self.agents = self.possible_agents[:]
        self.rewards = {agent: 0 for agent in self.agents}
        self._cumulative_rewards = {agent: 0 for agent in self.agents}
        self.dones = {agent: False for agent in self.agents}
        self.infos = {agent: {} for agent in self.agents}

        self.observations = {agent: self.observe(aidx) for aidx, agent in enumerate(self.agents)}
        '''
        Our agent_selector utility allows easy cyclic stepping through the agents list.
        '''
        self._agent_selector = agent_selector(self.agents)
        self.agent_selection = self._agent_selector.next()

    def step(self, action):
        # change input parameters so that only a few jobs, for each rsu, are enqueued each time

        # action[0] = processing multiplier
        # action[1] = jobs batch destination

        if self.dones[self.agent_selection]:
            # handles stepping an agent which is already done
            # accepts a None action for the one agent, and moves the agent_selection to
            # the next done agent,  or if there are no more done agents, to the next live agent
            return self._was_done_step(action)

        agent = self.agent_selection

        # The cumulative reward that the agent has received since it last acted.
        self._cumulative_rewards[agent] = 0

        self.steps += 1
        """
        # take action
        if self.alg == "MULTIAGENT":
            if self.shifting:  # increase or decrease current probability
                for i in range(len(self.drones)):
                    self.drones[i].set_processing(list(actions.values())[i][0])
                    self.drones[i].change_offloading_probability(INCREASE[list(actions.values())[i][1]])
                    self.offloading_probabilities.append(self.drones[i].offloading_prob)
                    self.processing_rates.append(self.drones[i].processing_rate)
            else:  # set probability to certain value
                for i in range(len(self.drones)):
                    self.drones[i].set_processing(list(actions.values())[i][0])
                    self.drones[i].set_offloading_probability(list(actions.values())[i][1])
                    self.offloading_probabilities.append(self.drones[i].offloading_prob)
                    self.processing_rates.append(self.drones[i].processing_rate)

        if self.alg == "ldo":
            for i in range(len(self.drones)):
                self.drones[i].set_processing(list(actions.values())[i][0])
                self.drones[i].set_offloading_probability(0)
                self.offloading_probabilities.append(self.drones[i].offloading_prob)
                self.processing_rates.append(self.drones[i].processing_rate)

        if self.alg == "us":
            for i in range(len(self.drones)):
                self.drones[i].set_offloading_probability(5)
                self.offloading_probabilities.append(self.drones[i].offloading_prob)
                self.processing_rates.append(self.drones[i].processing_rate)
        """
        [_, _, t_event] = self.time_matrix.search_next_event()

        # schedule all the jobs arrived in the last epochs for the agent
        job_to_schedule_counter = 0
        for job in self.jobs_to_schedule:  # has [id_drone, Packet(t_event)]
            if job[0] == self.agent_selection:
                job_to_schedule_counter += 1
                packet = job[1]
                drone_list = collections.deque(self.drones)
                drone_list.rotate(-agent)  # to shift to the left
                shifted_drone_list = list(drone_list)
                destination_drone_id = shifted_drone_list[action[1]].id
                print("agent:", self.agent_selection, " action:", action[1], " destination_drone_id:", destination_drone_id)
                # destination_drone_id is now returning the rotated id of the destination drone!
                rotated_id = action[1]
                packet.set_destination(destination_drone_id)  # set the drone destination
                self.drones[self.agent_selection].rotated_destination_id = rotated_id
                self.drones[self.zones[self.agent_selection].drone_id].job_arrival(self.agent_selection, t_event,
                                                                                   self.time_matrix, self.zones,
                                                                                   packet)
                self.jobs_to_schedule.remove(job)
        self.drones[self.agent_selection].scheduled_jobs = job_to_schedule_counter
        # collect reward if it is the last agent to act
        if self._agent_selector.is_last():
            assert not self.jobs_to_schedule  # check that there are no jobs left to schedule
            [_, _, t_event] = self.time_matrix.search_next_event()

            obs_timer = t_event
            while (t_event - obs_timer) < self.obs_max_timer:
                [row, column, t_event] = self.time_matrix.search_next_event()
                if column == 0:  # CHANGE ACTIVITY STATE
                    self.zones[row].change_zone_state(t_event, self.time_matrix)
                    if self.zones[row].state == 1:
                        self.count_cycle_zone[row] += 1

                elif column == 1:  # JOB ARRIVAL
                    # changed! Jobs are not immediately enqueued, but are inserted in a batch (jobs_to_schedule)
                    # to be scheduled at the start of the next decision epoch
                    # Therefore, jobs_arrival is actually done at the beginning of the decision epoch.
                    # The only thing that is updated here is the zones.schedule_next_arrival()
                    self.jobs_to_schedule.append([row, Packet(t_event)])
                    # schedule next arrival
                    self.zones[row].schedule_next_arrival(self.time_matrix, t_event)
                    # self.drones[self.zones[row].drone_id].job_arrival(row, t_event, self.time_matrix, self.zones)
                    self.drones[self.zones[row].drone_id].increase_counter()

                    self.update_normalization_counters()

                elif column == 2:  # JOB PROCESSING
                    tot_delay, proc_delay, off_delay = self.drones[row].job_processing(row, t_event, self.time_matrix,
                                                                                       self.zones)
                    self.update_metrics(tot_delay, proc_delay, off_delay)

                elif column == 3:  # JOB OFFLOADING
                    self.drones[row].job_offloading(row, t_event, self.time_matrix, self.zones, self.drones)

                self.t = t_event

            # update metrics (some jobs may be arrived to other queues via offloading event, which doesn't track
            # the receiving drone queues to update the metrics)
            self.update_normalization_counters()
            n_active_cpus = []
            for drone in self.drones:
                n_active_cpus.append(drone.processing_rate)

            delay_weight = self.delay_weight
            consumption_weight = self.consumption_weight
            # retrieve rewards
            mean_delay_rew = -statistics.mean(self.current_delay) * delay_weight
            mean_consumption_rew = -statistics.mean(n_active_cpus) * consumption_weight
            self.mean_delay_reward.append(mean_delay_rew)
            self.mean_consumption_reward.append(mean_consumption_rew)

            reward = mean_delay_rew + mean_consumption_rew
            self.mean_reward.append(reward)

            # rewards for all agents are placed in the .rewards dictionary
            self.rewards = {agent: reward for agent in self.agents}

            # The dones dictionary must be updated for all players.
            env_done = self.t >= self.max_time
            self.dones = {agent: env_done for agent in self.agents}

            # observe the current state

            for i in self.agents:
                self.observations[i] = {agent: self.observe(aidx) for aidx, agent in enumerate(self.agents)}

            self.tot_reward += reward
            self.current_delay = []

            for drone in self.drones:
                drone.clear_buffer()

            # typically there won't be any information in the infos, but there must
            # still be an entry for each agent
            infos = {agent: {} for agent in self.agents}

            if env_done:
                self.agents = []
                mean_delay = statistics.mean(self.delay)
                mean_reward = statistics.mean(self.mean_reward)
                jitter = statistics.variance(self.delay, mean_delay)
                off_probs = [drone.offloading_prob for drone in self.drones]
                mean_consumption = statistics.mean(self.mean_consumption_reward)
                mean_delay_rew = statistics.mean(self.mean_delay_reward)
                mean_proc_rate = statistics.mean(self.processing_rates)
                offloaded_pkts = sum([drone.offloaded_pkts for drone in self.drones])
                processed_pkts = sum([drone.processed_pkts for drone in self.drones])
                off_percentages = offloaded_pkts / (offloaded_pkts + processed_pkts)
                max_q = max([drone.max_queue_length for drone in self.drones])
                max_q_o = max([drone.max_ol_queue_length for drone in self.drones])
                mean_q = statistics.mean([drone.get_mean_queue()[0] for drone in self.drones])
                mean_q_o = statistics.mean([drone.get_mean_queue()[1] for drone in self.drones])
                if self.alg == "MULTIAGENT":
                    mean_off_probs = statistics.mean(self.offloading_probabilities)
                    wandb.log({"mean offloading probabilities": mean_off_probs}, commit=False)
                else:
                    mean_off_probs = -1
                lost_p = sum([drone.lost_pkts for drone in self.drones])
                arrived_p = sum([drone.arrived_pkts for drone in self.drones])
                lost_percentage = lost_p / arrived_p
                wandb.log({"episode reward": self.tot_reward}, commit=False)
                wandb.log({"offloading percentage": off_percentages}, commit=False)
                wandb.log({"lost packet percentage": lost_percentage}, commit=False)
                wandb.log({"max processing queue": max_q}, commit=False)
                wandb.log({"max offloading queue": max_q_o}, commit=False)
                wandb.log({"mean processing queue": mean_q}, commit=False)
                wandb.log({"mean offloading queue": mean_q_o}, commit=False)
                wandb.log({"mean processing rates": mean_proc_rate}, commit=False)
                wandb.log({"mean delay reward": mean_delay_rew}, commit=False)
                wandb.log({"mean reward": mean_reward}, commit=False)
                wandb.log({"mean consumption reward": mean_consumption}, commit=False)
                wandb.log({f"final offloading probability - {d_idx}": drone.offloading_prob
                           for d_idx, drone in enumerate(self.drones)}, commit=False)
                wandb.log({"jitter": jitter}, commit=False)
                wandb.log({"episode mean delay": mean_delay}, commit=True)

                if self.save_res:
                    self.res_buffer.save_run_results(avg_delay=mean_delay, jitter=jitter, reward=self.tot_reward,
                                                     offloading_ratio=mean_off_probs, lost_jobs=lost_percentage)
        else:

            # no rewards are allocated until all players give an action
            self._clear_rewards()  # for agent in self.rewards:
                                   #     self.rewards[agent] = 0
        # selects the next agent.
        self.agent_selection = self._agent_selector.next()
        # Adds .rewards to ._cumulative_rewards
        self._accumulate_rewards()  # for agent, reward in self.rewards.items():
                                    #     self._cumulative_rewards[agent] += reward

    def observe(self, agent):
        '''
        Observe should return the observation of the specified agent. This function
        should return a sane observation (though not necessarily the most up to date possible)
        at any time after reset() is called.
        '''

        drone_list = collections.deque(self.drones)
        drone_list.rotate(-agent)  # to shift to the left
        shifted_drone_list = list(drone_list)  # check if it is correct by printing it!
        print("(shifted)", shifted_drone_list, " \nvs\n(original)", self.drones)
        out = np.full((self.feature_size), 0.0)
        # observation of one agent is the previous state of the other

        personal_feature = 4
        
        drone = self.drones[agent]
        out[0] = drone.queue / self.max_observed_queue
        out[1] = drone.queue_ol / self.max_observed_queue_ol
        out[2] = drone.scheduled_jobs / self.max_observed_job_counter
        out[3] = drone.rotated_destination_id / self.number_of_uavs
        out[4] = drone.processing_rate / (drone.starting_processing_rate * self.max_number_of_cpus)
        
        for i in range(1, len(shifted_drone_list)):  # since the first element needs to be skipped
            out[i + personal_feature] = shifted_drone_list[i].queue / self.max_observed_queue

        for i in range(1, len(shifted_drone_list)):
            if i != agent:
                out[i + 2 * len(self.drones) + personal_feature] = shifted_drone_list[
                                                                   i].job_counter_obs / self.max_observed_job_counter

        # maybe to change into jobs in the offloading queues that are not still received by the uavs?

        for i in range(1, len(shifted_drone_list)):
            if i != agent:
                out[i + 3 * len(self.drones) + personal_feature] = shifted_drone_list[i].processing_rate / (
                            self.drones[i].starting_processing_rate * self.max_number_of_cpus)

        for i in range(1, len(shifted_drone_list)):
            if i != agent:
                out[i + 4 * len(self.drones) + personal_feature] = shifted_drone_list[i].rotated_destination_id / self.number_of_uavs
        out = np.array(out)

        return out
    """
    
    def get_obs(self, agent):
        personal_feature = 5
        out = np.full((self.feature_size), 0.0)
        drone = self.drones[agent]
        out[0] = drone.queue / self.max_observed_queue
        out[1] = drone.queue_ol / self.max_observed_queue_ol
        out[2] = drone.job_counter_obs / self.max_observed_job_counter
        out[3] = drone.offloading_prob / 100
        out[4] = drone.processing_rate / (drone.starting_processing_rate * self.max_number_of_cpus)

        for i in range(len(self.drones)):
            out[i + personal_feature] = self.drones[i].queue / self.max_observed_queue

        for i in range(len(self.drones)):
            out[i + len(self.drones) + personal_feature] = self.drones[i].queue_ol / self.max_observed_queue_ol

        for i in range(len(self.drones)):
            out[i + 2 * len(self.drones) + personal_feature] = self.drones[
                                                                   i].job_counter_obs / self.max_observed_job_counter

        for i in range(len(self.drones)):
            out[i + 3 * len(self.drones) + personal_feature] = self.drones[i].offloading_prob / 100

        for i in range(len(self.drones)):
            out[i + 4 * len(self.drones) + personal_feature] = self.drones[i].processing_rate / (self.drones[i].starting_processing_rate * self.max_number_of_cpus)
        out = np.array(out)

        return out
    """
    def update_normalization_counters(self):
        for i in range(len(self.drones)):
            if self.drones[i].queue > self.max_observed_queue:
                self.max_observed_queue = self.drones[i].queue
            if self.drones[i].queue_ol > self.max_observed_queue_ol:
                self.max_observed_queue_ol = self.drones[i].queue_ol
            if self.drones[i].scheduled_jobs > self.max_observed_job_counter:  # edited to keep track of scheduled jobs
                self.max_observed_job_counter = self.drones[i].scheduled_jobs

    def update_metrics(self, tot_delay, proc_delay, off_delay):

        self.delay.append(tot_delay)  # former delay_arr
        self.current_delay.append(tot_delay)  # to calculate mean delay of the whole network during epoch

        # need the try catch block since the max lenghts of the array (K/mu + Kol/muol)
        # is not the real possible max delay obtainable (because of esp_rand function)
        try:
            self.arr_delay[int(tot_delay)] += 1
        except:
            assert int(tot_delay) >= 0
            self.arr_delay.extend(((int(tot_delay) + 1) - len(self.arr_delay)) * [0])
            self.arr_delay[(int(tot_delay))] = 1

        delay = np.zeros(3)
        delay[0] = tot_delay
        delay[1] = proc_delay
        off = False
        if off_delay is not None:
            delay[2] = off_delay
            off = True
        for i in range(len(self.avg_tot_delay)):
            if i != 2 or off:
                self.counter_avg_td[i] += 1
                counter = self.counter_avg_td[i]
                avg_delay = self.avg_tot_delay[i]
                new_delay = delay[i]
                self.avg_tot_delay[i] = (((counter - 1) * avg_delay) + new_delay) / counter
