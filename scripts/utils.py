import os
import matplotlib.pyplot as plt

def plot_requests(time_index, df_duck, df_bus, plot_dir, step):
    plt.figure()
    plt.plot(df_duck.index, df_duck['demand'], label='Duck Demand')
    plt.plot(df_bus.index, df_bus['demand'], label='Bus Demand')
    plt.xlabel('Time')
    plt.ylabel('Demand')
    plt.title(f'Demand Curves at Time Slot {time_index[step]}')
    plt.legend()
    plt.savefig(os.path.join(plot_dir, f'demand_plot_{step + 1}.png'))
    plt.close()

def plot_bids_per_bidder(accepted_bids, non_accepted_bids, plot_dir):
    for bidder_id in accepted_bids.keys():
        plt.figure()
        accepted = accepted_bids[bidder_id]
        non_accepted = non_accepted_bids[bidder_id]

        if accepted:
            times = [bid['time_slot'] for bid in accepted]
            powers = [bid['power'] for bid in accepted]
            prices = [bid['price'] for bid in accepted]
            plt.scatter(times, powers, c=prices, cmap='viridis', label='Accepted Bids')
            plt.colorbar(label='Price')

        if non_accepted:
            times = [bid['time_slot'] for bid in non_accepted]
            powers = [bid['power'] for bid in non_accepted]
            prices = [bid['price'] for bid in non_accepted]
            plt.scatter(times, powers, c=prices, cmap='coolwarm', label='Non-Accepted Bids', marker='x')
            plt.colorbar(label='Price')

        plt.xlabel('Time')
        plt.ylabel('Power')
        plt.title(f'Bids for {bidder_id}')
        plt.legend()
        plt.savefig(os.path.join(plot_dir, f'bids_{bidder_id}.png'))
        plt.close()

def plot_successful_bids_per_bidder(all_accepted_bids, plot_dir):
    bidders = set()
    for bids in all_accepted_bids.values():
        for bid in bids:
            bidders.add(bid['bidder_id'])

    for bidder_id in bidders:
        times = []
        powers = []
        for time_slot, bids in all_accepted_bids.items():
            power = None
            for bid in bids:
                if bid['bidder_id'] == bidder_id:
                    power = bid['power']
                    break
            times.append(time_slot)
            powers.append(power)

        plt.figure()
        plt.plot(times, powers, label=f'Successful Bids for {bidder_id}')
        plt.xlabel('Time')
        plt.ylabel('Power')
        plt.title(f'Successful Bids for {bidder_id}')
        # plt.legend()
        plt.grid()
        plt.savefig(os.path.join(plot_dir, f'successful_bids_{bidder_id}.png'))
        plt.close()

def plot_unsuccessful_bids_per_bidder(all_not_accepted_bids, plot_dir):
    bidders = set()
    for bids in all_not_accepted_bids.values():
        for bid in bids:
            bidders.add(bid['bidder_id'])

    for bidder_id in bidders:
        times = []
        powers = []
        for time_slot, bids in all_not_accepted_bids.items():
            power = None
            for bid in bids:
                if bid['bidder_id'] == bidder_id:
                    power = bid['power']
                    break
            times.append(time_slot)
            powers.append(power)

        plt.figure()
        plt.plot(times, powers, label=f'Unsuccessful Bids for {bidder_id}')
        plt.xlabel('Time')
        plt.ylabel('Power')
        plt.title(f'Unsuccessful Bids for {bidder_id}')
        # plt.legend()
        plt.grid()
        plt.savefig(os.path.join(plot_dir, f'unsuccessful_bids_{bidder_id}.png'))
        plt.close()

def plot_combined_bids_per_bidder(all_accepted_bids, all_not_accepted_bids, plot_dir):
    bidders = set()
    for bids in all_accepted_bids.values():
        for bid in bids:
            bidders.add(bid['bidder_id'])
    for bids in all_not_accepted_bids.values():
        for bid in bids:
            bidders.add(bid['bidder_id'])

    for bidder_id in bidders:
        accepted_times = []
        accepted_powers = []
        non_accepted_times = []
        non_accepted_powers = []

        for bids in all_accepted_bids.values():
            for bid in bids:
                if bid['bidder_id'] == bidder_id:
                    accepted_times.append(bid['time_slot'])
                    accepted_powers.append(bid['power'])

        for bids in all_not_accepted_bids.values():
            for bid in bids:
                if bid['bidder_id'] == bidder_id:
                    non_accepted_times.append(bid['time_slot'])
                    non_accepted_powers.append(bid['power'])

        plt.figure()
        plt.scatter(accepted_times, accepted_powers, color='green', label='Accepted')
        plt.scatter(non_accepted_times, non_accepted_powers, color='red', label='Not-Accepted', marker='x')
        plt.xlabel('Time')
        plt.ylabel('Power')
        plt.title(f'Combined Bids for {bidder_id}')
        plt.legend()
        plt.grid()
        plt.savefig(os.path.join(plot_dir, f'combined_bids_{bidder_id}.png'))
        plt.close()

def plot_all_accepted_bids(all_accepted_bids, plot_dir):
    bidders = set()
    for bids in all_accepted_bids.values():
        for bid in bids:
            bidders.add(bid['bidder_id'])

    plt.figure()
    for bidder_id in bidders:
        times = []
        powers = []
        for time_slot, bids in all_accepted_bids.items():
            for bid in bids:
                if bid['bidder_id'] == bidder_id:
                    times.append(bid['time_slot'])
                    powers.append(bid['power'])
        # plt.plot(times, powers, label=f'{bidder_id}')
        plt.scatter(times, powers, label=f'{bidder_id}')

    plt.xlabel('Time')
    plt.ylabel('Power')
    plt.title('Accepted Bids')
    plt.legend()
    plt.grid()
    plt.savefig(os.path.join(plot_dir, 'all_accepted_bids.png'))
    plt.close()

def plot_buyer_requests_and_wtp(buyers, plot_dir):
    for buyer in buyers:
        power_requests = []
        wtp = []
        time_index = buyer.demand_curve.index
        for time_slot in time_index:
            power_requests.append(buyer.get_demand(time_slot).values[0])
            wtp.append(buyer.willingness_to_pay)

        fig, ax1 = plt.subplots()

        color = 'tab:blue'
        ax1.set_xlabel('Time')
        ax1.set_ylabel('Power Request', color=color)
        ax1.plot(time_index, power_requests, color=color)
        ax1.tick_params(axis='y', labelcolor=color)
        ax1.grid()

        ax2 = ax1.twinx()
        color = 'tab:red'
        ax2.set_ylabel('Willingness to Pay', color=color)
        ax2.plot(time_index, wtp, color=color)
        ax2.tick_params(axis='y', labelcolor=color)
        ax2.grid()

        plt.title(f'Power Request and Willingness to Pay for {buyer.id}')
        fig.tight_layout()
        plt.savefig(os.path.join(plot_dir, f'buyer_requests_wtp_{buyer.id}.png'))
        plt.close()

def plot_rewards_per_bidder(all_accepted_bids, plot_dir):
    bidders_rewards = {}

    # Collect rewards for each bidder
    for time_slot, bids in all_accepted_bids.items():
        for bid in bids:
            bidder_id = bid['bidder_id']
            reward = bid['reward']
            if bidder_id not in bidders_rewards:
                bidders_rewards[bidder_id] = {'time_slots': [], 'rewards': []}
            bidders_rewards[bidder_id]['time_slots'].append(time_slot)
            bidders_rewards[bidder_id]['rewards'].append(reward)

    # Plot rewards for each bidder
    for bidder_id, data in bidders_rewards.items():
        plt.figure()
        plt.plot(data['time_slots'], data['rewards'], label=f'Rewards for {bidder_id}')
        plt.xlabel('Time Step')
        plt.ylabel('Reward')
        plt.title(f'Rewards for {bidder_id}')
        # plt.legend()
        plt.grid()
        plt.savefig(os.path.join(plot_dir, f'rewards_{bidder_id}.png'))
        plt.close()


def plot_all_bidders_rewards(all_accepted_bids, plot_dir):
    bidders_rewards = {}

    # Collect rewards for each bidder
    for time_slot, bids in all_accepted_bids.items():
        for bid in bids:
            bidder_id = bid['bidder_id']
            reward = bid['reward']
            if bidder_id not in bidders_rewards:
                bidders_rewards[bidder_id] = {'time_slots': [], 'rewards': []}
            bidders_rewards[bidder_id]['time_slots'].append(time_slot)
            bidders_rewards[bidder_id]['rewards'].append(reward)

    # Plot rewards for all bidders on the same graph
    plt.figure()
    for bidder_id, data in bidders_rewards.items():
        plt.plot(data['time_slots'], data['rewards'], label=f'{bidder_id}')

    plt.xlabel('Time Step')
    plt.ylabel('Reward')
    plt.title('Rewards for All Bidders')
    plt.legend()
    plt.grid()
    plt.savefig(os.path.join(plot_dir, 'all_bidders_rewards.png'))
    plt.close()

def plot_flexibility_from_history(market_operator, plot_dir):
    """
    Plot the bidded flexibility and the real provided flexibility for each bidder using clearing_results_history.

    :param market_operator: MarketOperator object
    :param plot_dir: Directory to save the plots
    """
    bidder_data = {}

    # Aggregate data for each bidder
    for time_slot, results in market_operator.clearing_results_history.items():
        for result in results:
            allocations = result['allocations']
            for allocation in allocations:
                bidder_id = allocation['bidder_id']
                if bidder_id not in bidder_data:
                    bidder_data[bidder_id] = {'time_slots': [], 'bidded_flexibility': [], 'real_flexibility': []}
                bidder_data[bidder_id]['time_slots'].append(time_slot)
                bidder_data[bidder_id]['bidded_flexibility'].append(allocation['bidded_flexibility'])
                bidder_data[bidder_id]['real_flexibility'].append(allocation['provided_flexibility'])

    # Plot data for each bidder
    for bidder_id, data in bidder_data.items():
        plt.figure(figsize=(10, 6))
        plt.plot(data['time_slots'], data['bidded_flexibility'], label='Bidded')
        plt.plot(data['time_slots'], data['real_flexibility'], label='Provided')
        plt.xlabel('Time Slot')
        plt.ylabel('Flexibility [MW]')
        plt.title(f'Flexibility {bidder_id}')
        plt.legend()
        plt.grid(True)
        plt.savefig(f"{plot_dir}/flexibility_{bidder_id}.png")
        plt.close()

import matplotlib.pyplot as plt

def plot_flexibility_and_rewards_from_history(market_operator, plot_dir):
    """
    Plot the bidded flexibility, real provided flexibility, and rewards for each bidder using clearing_results_history.

    :param market_operator: MarketOperator object
    :param plot_dir: Directory to save the plots
    """
    bidder_data = {}

    # Aggregate data for each bidder
    for time_slot, results in market_operator.clearing_results_history.items():
        for result in results:
            allocations = result['allocations']
            for allocation in allocations:
                bidder_id = allocation['bidder_id']
                if bidder_id not in bidder_data:
                    bidder_data[bidder_id] = {'time_slots': [], 'bidded_flexibility': [], 'real_flexibility': [], 'rewards': []}
                bidder_data[bidder_id]['time_slots'].append(time_slot)
                bidder_data[bidder_id]['bidded_flexibility'].append(allocation['bidded_flexibility'])
                bidder_data[bidder_id]['real_flexibility'].append(allocation['provided_flexibility'])
                bidder_data[bidder_id]['rewards'].append(allocation['reward'])

    # Plot data for each bidder
    for bidder_id, data in bidder_data.items():
        fig, ax1 = plt.subplots(figsize=(10, 6))

        ax1.set_xlabel('Time Slot')
        ax1.set_ylabel('Flexibility (kW)', color='tab:blue')
        ax1.plot(data['time_slots'], data['bidded_flexibility'], label='Bidded Flexibility', color='tab:blue')
        ax1.plot(data['time_slots'], data['real_flexibility'], label='Real Provided Flexibility', color='tab:green')
        ax1.tick_params(axis='y', labelcolor='tab:blue')
        ax1.legend(loc='upper left')
        ax1.grid(True)

        ax2 = ax1.twinx()
        ax2.set_ylabel('Rewards', color='tab:red')
        ax2.plot(data['time_slots'], data['rewards'], label='Rewards', color='tab:red')
        ax2.tick_params(axis='y', labelcolor='tab:red')
        ax2.legend(loc='upper right')

        plt.title(f'Flexibility and Rewards for Bidder {bidder_id}')
        fig.tight_layout()
        plt.savefig(f"{plot_dir}/flexibility_rewards_{bidder_id}.png")
        plt.close()