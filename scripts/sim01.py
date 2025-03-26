import numpy as np
import pandas as pd
import os
from datetime import datetime, timedelta
from classes.buyer import Buyer
from classes.bidder import Bidder
from classes.market_operator import MarketOperator
from scripts.utils import (plot_successful_bids_per_bidder, plot_unsuccessful_bids_per_bidder,
                           plot_combined_bids_per_bidder, plot_all_accepted_bids, plot_buyer_requests_and_wtp)

def test_market_simulation():
    # Initialize simulation parameters
    num_days = 1
    slots_per_day = 96
    num_slots = num_days * slots_per_day
    clearing_steps = 24
    time_index = pd.date_range(start="2025-01-01", periods=num_slots, freq="15min")

    # Create directory for saving plots
    plot_dir = "../data/plots"
    os.makedirs(plot_dir, exist_ok=True)

    # Create Buyers with demand curves
    duck_base = np.array([
        0.4, 0.3, 0.25, 0.25, 0.3, 0.3, 0.2, 0.2, 0.2, 0.2, 0.2, 0.25,
        0.3, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.8, 0.8, 0.7,
        0.6, 0.5, 0.4, 0.35, 0.3, 0.3, 0.3, 0.25, 0.2, 0.15, 0.1, 0.1,
        0.1, 0.1, 0.2, 0.3, 0.4, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8, 0.85,
        0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.9, 0.9, 0.8, 0.8,
        0.7, 0.7, 0.7, 0.6, 0.5, 0.4, 0.4, 0.35, 0.3, 0.2, 0.2, 0.2,
        0.25, 0.3, 0.3, 0.4, 0.5, 0.7, 0.9, 1.0, 1.1, 1.2, 1.2, 1.1,
        1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.5, 0.4, 0.3, 0.2, 0.2, 0.2
    ])
    duck_base = np.tile(duck_base, num_days)
    df_duck = pd.DataFrame({'demand': duck_base}, index=time_index)

    bus_base = np.array([
        0.0, 0.0, 0.0, 0.2, 0.5, 0.5, 0.5, 0.5, 0.3, 0.3, 0.3, 0.2,
        0.2, 0.2, 0.3, 0.4, 0.5, 1.0, 1.2, 1.2, 1.2, 1.2, 1.2, 1.0,
        0.8, 0.6, 0.3, 0.3, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.5, 0.5,
        1.0, 1.5, 1.5, 1.5, 1.5, 1.2, 1.0, 0.8, 0.8, 0.8, 0.8, 0.8,
        0.8, 0.8, 0.8, 0.8, 1.0, 1.0, 1.0, 1.0, 1.2, 1.2, 1.2, 1.0,
        0.8, 0.6, 0.6, 0.5, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.8, 1.0,
        1.0, 1.2, 1.2, 1.2, 1.2, 1.0, 0.8, 0.6, 0.5, 0.5, 0.4, 0.3,
        0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.4, 0.6, 0.8, 1.0
    ])
    bus_base = np.tile(bus_base, num_days)
    df_bus = pd.DataFrame({'demand': bus_base}, index=time_index)

    buyers = [
        Buyer("BUYER_1", df_duck, willingness_to_pay=50.0),
        Buyer("BUYER_2", df_bus, willingness_to_pay=55.0)
    ]

    # Create Bidders
    b1 = Bidder(id="BIDDER_01", alpha=0.02, beta=0.02, gamma=0.5, L=7, w1=1.0, w2=1.0, w3=1.0, baseline=50)
    b1.pow_base = 4.0
    b1.pow_bid = 3.0

    b2 = Bidder(id="BIDDER_02", alpha=0.05, beta=0.03, gamma=0.7, L=10, w1=1.5, w2=0.8, w3=0.8, baseline=80)
    b2.pow_base = 7.0
    b2.pow_bid = 5.0

    b3 = Bidder(id="BIDDER_03", alpha=0.03, beta=0.03, gamma=0.4, L=5, w1=1.0, w2=1.2, w3=1.0, baseline=100)
    b3.pow_base = 10.0
    b3.pow_bid = 8.0

    bidders = [b1, b2, b3]

    # Initialize MarketOperator
    market_op = MarketOperator(alpha_rem=5.0, beta_rem=-2.0, threshold_rem=0.1, power_ref=100, price_ref=20)

    # Main simulation loop
    all_accepted_bids = {}
    all_not_accepted_bids = {}
    for step, time_slot in enumerate(time_index):
        print(f"*** Starting simulating time slot: {time_slot}")
        # Update reference values for each bidder
        pow_req_ref = market_op.average_last_n_requested_powers(7)
        avg_acc_ref = market_op.average_last_n_accepted_prices(7)

        # Buyers requests
        current_buyer_requests = []
        print("Flexibility requests:")
        for buyer in buyers:
            request_info = {
                'id': buyer.id,
                'requested_power': buyer.get_demand(time_slot).values[0],
                'wtp': buyer.willingness_to_pay
            }
            market_op.receive_buyer_request(time_slot, request_info)
            current_buyer_requests.append(request_info)
            print("Request info:", request_info)

        # Bidders bids
        print("Flexibility bidding:")
        for bidder in bidders:
            bidder.set_reference_values(pow_req_ref, avg_acc_ref)
            best_buyer = bidder.select_buyer(current_buyer_requests)
            p_start, final_power = bidder.build_offer(best_buyer['id'], best_buyer['requested_power'], bidder.pow_bid)
            bidder.update_current_bidding(buyer_id=best_buyer['id'], offered_power=final_power, offered_price=p_start)
            market_op.receive_bid_from_bidder(time_slot=time_slot, bid_info=bidder.current_bidding)
            print("Bid info:", bidder.current_bidding)

        # Solve the market every 24 steps
        if (step + 1) % clearing_steps == 0:
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

    # Plot and save the successful bids for each bidder
    plot_successful_bids_per_bidder(all_accepted_bids, plot_dir)

    # Plot and save the unsuccessful bids for each bidder
    plot_unsuccessful_bids_per_bidder(all_not_accepted_bids, plot_dir)

    # Plot and save the combined bids for each bidder
    plot_combined_bids_per_bidder(all_accepted_bids, all_not_accepted_bids, plot_dir)

    # Plot and save all accepted bids
    plot_all_accepted_bids(all_accepted_bids, plot_dir)

    # Plot and save buyer requests and willingness to pay
    plot_buyer_requests_and_wtp(buyers, plot_dir)

if __name__ == "__main__":
    test_market_simulation()