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