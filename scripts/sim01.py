import numpy as np
import pandas as pd
import os
from classes.buyer import Buyer
from classes.bidder import Bidder
from classes.market_operator import MarketOperator
from classes.metering_agent import MeteringAgent
from scripts.utils import (plot_successful_bids_per_bidder, plot_unsuccessful_bids_per_bidder,
                           plot_combined_bids_per_bidder, plot_all_accepted_bids, plot_buyer_requests_and_wtp,
                           plot_rewards_per_bidder, plot_all_bidders_rewards, plot_flexibility_from_history,
                           plot_flexibility_and_rewards_from_history, plot_flexibility_requested_and_rewards_from_history)
from scripts.utils_baselines import create_residential_like_pattern, create_office_like_pattern, create_battery_pattern
from utils_baselines import create_duck_curve_pattern, create_bus_curve_pattern

def test_market_simulation():
    # Initialize simulation parameters
    num_days = 2
    slots_per_day = 96
    num_slots = num_days * slots_per_day
    clearing_steps = 24
    time_index = pd.date_range(start="2025-01-01", periods=num_slots, freq="15min")

    # Create directory for saving plots
    plot_dir = "../data/plots"
    os.makedirs(plot_dir, exist_ok=True)

    # Create Buyers with demand curves
    duck_curve_data = create_duck_curve_pattern(time_index, num_days)
    bus_curve_data = create_bus_curve_pattern(time_index, num_days)

    buyers = [
        Buyer("BUYER_1", duck_curve_data, willingness_to_pay=50.0),
        Buyer("BUYER_2", bus_curve_data, willingness_to_pay=55.0)
    ]

    # Generate baselines for bidders
    residential_baseline = create_residential_like_pattern(time_index)
    office_baseline = create_office_like_pattern(time_index)
    battery_baseline = create_battery_pattern(time_index)

    # Create Bidders
    # b1 = Bidder(id="BIDDER_01", alpha=0.02, beta=0.02, gamma=0.5, L=7, w1=1.0, w2=1.0, w3=1.0, baseline=residential_baseline)
    # b2 = Bidder(id="BIDDER_02", alpha=0.05, beta=0.03, gamma=0.7, L=10, w1=1.5, w2=0.8, w3=0.8, baseline=office_baseline)
    # b3 = Bidder(id="BIDDER_03", alpha=0.03, beta=0.03, gamma=0.4, L=5, w1=1.0, w2=1.2, w3=1.0, baseline=battery_baseline)
    b1 = Bidder(id="BIDDER_01", alpha=0, beta=0, gamma=0, L=7, w1=1.0, w2=1.0, w3=1.0, baseline=residential_baseline)
    b2 = Bidder(id="BIDDER_02", alpha=0, beta=0, gamma=0, L=10, w1=1.0, w2=1.0, w3=1.0, baseline=office_baseline)
    b3 = Bidder(id="BIDDER_03", alpha=0, beta=0, gamma=0, L=5, w1=1.0, w2=1.0, w3=1.0, baseline=battery_baseline)

    bidders = [b1, b2, b3]

    # Initialize MeteringAgent
    metering_agent = MeteringAgent()
    # todo: Now we cycle only on the bidders but other meter point, not related to the bidders, could be added
    for bidder in bidders:
        metering_agent.add_metering_point(bidder.id)

    # Initialize MarketOperator
    # market_op = MarketOperator(alpha_rem=5.0, beta_rem=-2.0, gamma_rem=0.5, threshold_rem=0.1, threshold_rem_bid_inf=0.25, power_ref=100, price_ref=20)
    market_op = MarketOperator(alpha_rem=1, beta_rem=0, gamma_rem=0, threshold_rem=0.1, threshold_rem_bid_inf=0.25, power_ref=100, price_ref=20)

    # Main simulation loop
    all_accepted_bids = {}
    all_not_accepted_bids = {}
    for step, time_slot in enumerate(time_index):
        print(f"*** Starting simulating time slot: {time_slot}")
        # Update reference values for each bidder
        pow_req_ref = market_op.average_last_n_requested_powers(7)
        avg_acc_ref = market_op.average_last_n_accepted_prices(7)

        # STEP 1: Bidders update their baselines
        # Baselines update in the market operator is now reusing always the same values. This is a simplification,
        # in a real scenario, the baselines about the future steps should be updated considering the last forecast.
        # todo: Change the code below in order to update the baselines, considering the last forecast for the future steps
        print("Baselines updating")
        for bidder in bidders:
            market_op.store_bidder_baseline(bidder.id, bidder.baseline)

        # STEP 2: Buyers requesting flexibility
        current_buyer_requests = []
        print("Flexibility requests storage")
        for buyer in buyers:
            request_info = {
                'id': buyer.id,
                'requested_power': buyer.get_demand(time_slot).values[0],
                'wtp': buyer.willingness_to_pay
            }
            market_op.receive_buyer_request(time_slot, request_info)
            current_buyer_requests.append(request_info)
            print("Request info:", request_info)

        # STEP 3: Bidders offering flexibility to the buyers
        # Store bidding in market operator
        print("Flexibility bidding storage")
        for bidder in bidders:
            bidder.set_reference_values(pow_req_ref, avg_acc_ref)
            best_buyer = bidder.select_buyer(current_buyer_requests)
            p_start, final_power = bidder.build_offer(buyer_id=best_buyer['id'],
                                                      pow_req=best_buyer['requested_power'],
                                                      pow_bid=bidder.baseline.loc[time_slot, 'value']*0.8)
            bidder.update_current_bidding(buyer_id=best_buyer['id'], offered_power=final_power, offered_price=p_start)
            market_op.receive_bid_from_bidder(time_slot=time_slot, bid_info=bidder.current_bidding)
            print("Bid info:", bidder.current_bidding)

        # STEP 4: Metering agent stores actual values
        # Store actual values in metering agent
        print("Actual values storage")
        # todo: Now we cycle only on the bidders but other actual values for the market operator should be added
        for bidder in bidders:
            actual_value = bidder.baseline.loc[time_slot, 'value'] - bidder.current_bidding['power'] * (1 + 0.1 * np.random.randn())
            metering_agent.add_energy_measure(bidder.id, time_slot, actual_value)

        # STEP 5: Send information about the actual values to the bidders and the market operator
        # Store actual values related to the bidding in market operator and bidders
        for bidder in bidders:
            bidder.add_actual_value(time_slot, metering_agent.get_energy_data(bidder.id).loc[time_slot, 'energy'])
            market_op.store_bidder_actual(bidder.id, time_slot, metering_agent.get_energy_data(bidder.id).loc[time_slot, 'energy'])

        # Solve the market every clearing_steps steps
        if (step + 1) % clearing_steps == 0:
            # STEP 6: Market clearing
            print(f"Time slot: {time_slot}: Starting clearing the last {clearing_steps} steps.")
            accepted_bids, non_accepted_bids = market_op.pay_as_bid_market_solving(clearing_steps)

            # Update global bids dictionaries
            for ts, bids in accepted_bids.items():
                for bid in bids:
                    bid['time_slot'] = ts
                    all_accepted_bids.setdefault(ts, []).append(bid)
            for ts, bids in non_accepted_bids.items():
                for bid in bids:
                    bid['time_slot'] = ts
                    all_not_accepted_bids.setdefault(ts, []).append(bid)

            # Update bidders' history and tag time slots as cleared
            for bidder in bidders:
                for bid in accepted_bids.get(time_slot, []):
                    if bid['bidder_id'] == bidder.id:
                        bidder.update_history(bid['buyer_id'], time_slot, bid['price'], bid['power'], True)
                for bid in non_accepted_bids.get(time_slot, []):
                    if bid['bidder_id'] == bidder.id:
                        bidder.update_history(bid['buyer_id'], time_slot, bid['price'], bid['power'], False)
            print(f"Time slot: {time_slot}: Ending clearing the last {clearing_steps} steps.")
        print(f"*** Ending simulating time slot: {time_slot}")

    print("Printing plots in", plot_dir)

    # # Plot and save the successful bids for each bidder
    # plot_successful_bids_per_bidder(all_accepted_bids, plot_dir)
    #
    # # Plot and save the unsuccessful bids for each bidder
    # plot_unsuccessful_bids_per_bidder(all_not_accepted_bids, plot_dir)
    #
    # # Plot and save the combined bids for each bidder
    # plot_combined_bids_per_bidder(all_accepted_bids, all_not_accepted_bids, plot_dir)
    #
    # # Plot and save all accepted bids
    # plot_all_accepted_bids(all_accepted_bids, plot_dir)
    #
    # # PLot and save rewards for each bidder
    # plot_rewards_per_bidder(all_accepted_bids, plot_dir)
    #
    # # Plot and save buyer requests and willingness to pay
    # plot_buyer_requests_and_wtp(buyers, plot_dir)
    #
    # # Plot and save all bidders rewards
    # plot_all_bidders_rewards(all_accepted_bids, plot_dir)
    #
    # # Plot and save flexibility activation for each bidder
    # plot_flexibility_from_history(market_op, plot_dir)
    #
    # # Plot and save flexibility activation and rewards for each bidder
    # plot_flexibility_and_rewards_from_history(market_op, plot_dir)

    # Plot and save flexibility requested and rewards for each bidder
    plot_flexibility_requested_and_rewards_from_history(market_op, plot_dir)

if __name__ == "__main__":
    # todo (listed by priority):
    #   - Marginal costs analysis: how much would my flexibility activation cost? (Bidder)
    test_market_simulation()