#!/usr/bin/env python3
import gym
import ptan
import numpy as np
import argparse
import collections
from tensorboardX import SummaryWriter

import torch
import torch.nn.utils as nn_utils
import torch.nn.functional as F
import torch.optim as optim
import torch.multiprocessing as mp
from torch.autograd import Variable

from lib import common

GAMMA = 0.99
LEARNING_RATE = 0.001
ENTROPY_BETA = 0.01
BATCH_SIZE = 128

REWARD_STEPS = 4
CLIP_GRAD = 0.1

PROCESSES_COUNT = 4
NUM_ENVS = 12


def make_env():
    return ptan.common.wrappers.wrap_dqn(gym.make("PongNoFrameskip-v4"))

TotalReward = collections.namedtuple('TotalReward', field_names='reward')


def data_func(net, cuda, train_queue):
    envs = [make_env() for _ in range(NUM_ENVS)]
    agent = ptan.agent.PolicyAgent(lambda x: net(x)[0], cuda=cuda, apply_softmax=True)
    exp_source = ptan.experience.ExperienceSourceFirstLast(envs, agent, gamma=GAMMA, steps_count=REWARD_STEPS)

    for exp in exp_source:
        new_rewards = exp_source.pop_total_rewards()
        if new_rewards:
            train_queue.put(TotalReward(reward=np.mean(new_rewards)))
        train_queue.put(exp)

    train_queue.put(None)


def unpack_batch(batch, net, cuda=False):
    """
    Convert batch into training tensors
    :param batch:
    :param net:
    :return: states variable, actions tensor, reference values variable
    """
    states = []
    actions = []
    rewards = []
    not_done_idx = []
    last_states = []
    for idx, exp in enumerate(batch):
        states.append(np.array(exp.state, copy=False))
        actions.append(int(exp.action))
        rewards.append(exp.reward)
        if exp.last_state is not None:
            not_done_idx.append(idx)
            last_states.append(np.array(exp.last_state, copy=False))
    states_v = Variable(torch.from_numpy(np.array(states, copy=False)))
    actions_t = torch.LongTensor(actions)
    if cuda:
        states_v = states_v.cuda()
        actions_t = actions_t.cuda()

    # handle rewards
    rewards_np = np.array(rewards, dtype=np.float32)
    if not_done_idx:
        last_states_v = Variable(torch.from_numpy(np.array(last_states, copy=False)), volatile=True)
        if cuda:
            last_states_v = last_states_v.cuda()
        last_vals_v = net(last_states_v)[1]
        last_vals_np = last_vals_v.data.cpu().numpy()[:, 0]
        rewards_np[not_done_idx] += GAMMA ** REWARD_STEPS * last_vals_np

    ref_vals_v = Variable(torch.from_numpy(rewards_np))
    if cuda:
        ref_vals_v = ref_vals_v.cuda()

    return states_v, actions_t, ref_vals_v


if __name__ == "__main__":
    mp.set_start_method('spawn')
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action="store_true", help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    args = parser.parse_args()

    writer = SummaryWriter(comment="-a3c-data_pong_" + args.name)

    env = make_env()
    net = common.AtariA2C(env.observation_space.shape, env.action_space.n)
    if args.cuda:
        net.cuda()

    optimizer = optim.Adam(net.parameters(), lr=LEARNING_RATE, eps=1e-3)

    train_queue = mp.Queue(maxsize=PROCESSES_COUNT)
    data_proc_list = []
    for _ in range(PROCESSES_COUNT):
        data_proc = mp.Process(target=data_func, args=(net, args.cuda, train_queue))
        data_proc.start()
        data_proc_list.append(data_proc)

    batch = []
    step_idx = 0

    with common.RewardTracker(writer, stop_reward=18) as tracker:
        with ptan.common.utils.TBMeanTracker(writer, batch_size=10) as tb_tracker:
            while True:
                train_entry = train_queue.get()
                if train_entry is None:
                    break
                if isinstance(train_entry, TotalReward):
                    if tracker.reward(train_entry.reward, step_idx):
                        break
                    continue

                step_idx += 1

                batch.append(train_entry)
                if len(batch) < BATCH_SIZE:
                    continue

                states_v, actions_t, vals_ref_v = unpack_batch(batch, net, cuda=args.cuda)
                batch.clear()

                optimizer.zero_grad()
                logits_v, value_v = net(states_v)

                loss_value_v = F.mse_loss(value_v, vals_ref_v)

                log_prob_v = F.log_softmax(logits_v)
                adv_v = vals_ref_v - value_v.detach()
                log_prob_actions_v = adv_v * log_prob_v[range(BATCH_SIZE), actions_t]
                loss_policy_v = -log_prob_actions_v.mean()

                prob_v = F.softmax(logits_v)
                entropy_loss_v = ENTROPY_BETA * (prob_v * log_prob_v).sum(dim=1).mean()

                # calculate policy gradients only
                loss_policy_v.backward(retain_graph=True)
                grads = np.concatenate([p.grad.data.cpu().numpy().flatten()
                                        for p in net.parameters()
                                        if p.grad is not None])

                # apply entropy and value gradients
                loss_v = entropy_loss_v + loss_value_v
                loss_v.backward()
                nn_utils.clip_grad_norm(net.parameters(), CLIP_GRAD)
                optimizer.step()
                # get full loss
                loss_v += loss_policy_v

                tb_tracker.track("advantage", adv_v, step_idx)
                tb_tracker.track("values", value_v, step_idx)
                tb_tracker.track("batch_rewards", vals_ref_v, step_idx)
                tb_tracker.track("loss_entropy", entropy_loss_v, step_idx)
                tb_tracker.track("loss_policy", loss_policy_v, step_idx)
                tb_tracker.track("loss_value", loss_value_v, step_idx)
                tb_tracker.track("loss_total", loss_v, step_idx)

                tb_tracker.track("grad_l2", np.sqrt(np.mean(np.square(grads))), step_idx)
                tb_tracker.track("grad_max", np.max(np.abs(grads)), step_idx)
                tb_tracker.track("grad_var", np.var(grads), step_idx)

pass
