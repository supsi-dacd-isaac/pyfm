import pandas as pd

class MeteringAgent:
    """
    A class to gather and store energy measures for each metering point.
    """

    def __init__(self):
        """
        Initialize the MeteringAgent with an empty dictionary to store energy measures.
        """
        self.data = {}

    def add_metering_point(self, metering_point_id):
        """
        Add a new metering point with an empty DataFrame.

        :param metering_point_id: Identifier of the metering point
        """
        if metering_point_id not in self.data:
            self.data[metering_point_id] = pd.DataFrame(columns=['energy'])

    def add_energy_measure(self, metering_point_id, time_slot, energy):
        """
        Add an energy measure for a specific metering point and time slot.

        :param metering_point_id: Identifier of the metering point
        :param time_slot: The time (or slot) of the energy measure
        :param energy: The energy measure to be added
        """
        if metering_point_id not in self.data:
            self.add_metering_point(metering_point_id)
        self.data[metering_point_id].loc[time_slot, 'energy'] = energy

    def get_energy_data(self, metering_point_id):
        """
        Get the energy data for a specific metering point.

        :param metering_point_id: Identifier of the metering point
        :return: A DataFrame with the energy data for the specified metering point
        """
        return self.data.get(metering_point_id, pd.DataFrame(columns=['energy']))