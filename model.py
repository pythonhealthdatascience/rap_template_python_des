# ---
# This code is adapted from Sammi Rosser and Dan Chalk (2024) HSMA - the little
# book of DES (<https://github.com/hsma-programme/hsma6_des_book>).
# ---

from dataclasses import dataclass
from joblib import Parallel, delayed
import numpy as np
import pandas as pd
import simpy


@dataclass
class Defaults:
    '''Default parameters.'''
    # Inter-arrival times
    patient_inter = 5

    # Activity times
    mean_n_consult_time = 35

    # Resource numbers
    number_of_nurses = 9

    # Simulation meta parameters
    sim_duration = 600
    warm_up_period = 0
    number_of_runs = 5
    audit_interval = 5
    scenario_name = 0


@dataclass
class Patient:
    '''Represents a patient.'''
    def __init__(self, p_id):
        self.id = p_id
        self.q_time_nurse = 0


class Exponential:
    '''Exponential distribution.'''
    def __init__(self, mean, random_seed=None):
        self.mean = mean
        self.rand = np.random.default_rng(random_seed)

    def sample(self, size=None):
        '''Generate sample.'''
        return self.rand.exponential(self.mean, size=size)


class Model:
    '''Simulation model for a clinic.'''
    def __init__(self, param, run_number=0):
        # Store the model parameters
        self.param = param

        # Create SimPy environment
        self.env = simpy.Environment()

        self.patient_counter = 0
        self.nurse = simpy.Resource(self.env,
                                    capacity=self.param.number_of_nurses)
        self.run_number = run_number
        self.nurse_time_used = 0

        # Create list to store patient-level and interval audit results
        self.results_list = []
        self.utilisation_audit = []

        # Generate seeds based on run_number as entropy (the "starter" seed)
        # The seeds produced will create independent streams
        ss = np.random.SeedSequence(entropy=self.run_number)
        seeds = ss.spawn(2)

        # Initialise distributions using those seeds
        self.patient_inter_arrival_dist = Exponential(
            mean=self.param.patient_inter, random_seed=seeds[0])
        self.nurse_consult_time_dist = Exponential(
            mean=self.param.mean_n_consult_time, random_seed=seeds[1])

    def generator_patient_arrivals(self):
        '''Generates patient arrivals.'''
        while True:
            self.patient_counter += 1
            # Create new patient
            p = Patient(self.patient_counter)
            # Start process of attending clinic
            self.env.process(self.attend_clinic(p))
            # Sample and pass time to next arrival
            sampled_inter = self.patient_inter_arrival_dist.sample()
            yield self.env.timeout(sampled_inter)

    def attend_clinic(self, patient):
        '''Simulates patient journey through the clinic.'''
        # Start queueing and request nurse resource
        start_q_nurse = self.env.now
        with self.nurse.request() as req:
            yield req

            # Record time spent waiting
            end_q_nurse = self.env.now
            patient.q_time_nurse = end_q_nurse - start_q_nurse

            # Sample time spent with nurse
            sampled_nurse_act_time = self.nurse_consult_time_dist.sample()

            # Only save results if the warm up period has passed
            if self.env.now >= self.param.warm_up_period:
                # Save patient results to results_list
                self.results_list.append({
                    'patient_id': patient.id,
                    'q_time_nurse': patient.q_time_nurse,
                    'time_with_nurse': sampled_nurse_act_time
                })
                # Update total nurse time used - but if consultation would
                # overrun simulation, just use time to simulation end
                remaining_time = (self.param.warm_up_period + 
                                  self.param.sim_duration) - self.env.now
                self.nurse_time_used += min(
                    sampled_nurse_act_time, remaining_time)

            # Pass time spent with nurse
            yield self.env.timeout(sampled_nurse_act_time)

    def interval_audit_utilisation(self, resources, interval=1):
        '''
        Record resource utilisation at regular intervals.
        Need to pass to env.process.

        Parameters:
        -----------
            resource (list of dicts):
                A list of dictionaries in the format:
                [{'name': 'name', 'object': resource}]
            interval (int, optional):
                Time between audits (default: 1).
        '''
        while True:
            # Only save results if the warm up period has passed
            if self.env.now >= self.param.warm_up_period:
                # Collect data for each resource
                for resource in resources:
                    self.utilisation_audit.append({
                        'resource_name': resource['name'],
                        'simulation_time': self.env.now,
                        'number_utilised': resource['object'].count,
                        'number_available': resource['object'].capacity,
                        'queue_length': len(resource['object'].queue)
                    })
            # Trigger next audit after desired interval has passed
            yield self.env.timeout(interval)

    def run(self):
        '''Executes the simulation run.'''
        # Start patient generator
        self.env.process(self.generator_patient_arrivals())

        # Start interval auditor for nurse utilisation
        self.env.process(self.interval_audit_utilisation(
            resources=[{'name': 'nurse', 'object': self.nurse}],
            interval=self.param.audit_interval))

        # Run for specified duration + warm-up period
        self.env.run(until=self.param.sim_duration + self.param.warm_up_period)


class Trial:
    '''Manages multiple simulation runs.'''
    def __init__(self, param=Defaults()):
        '''
        If no param class provided, will create new instance of Defaults()
        '''
        # Store model parameters
        self.param = param
        # Initialise empty dataframes to store results
        self.patient_results_df = pd.DataFrame()
        self.trial_results_df = pd.DataFrame()
        self.interval_audit_df = pd.DataFrame()

    def run_single(self, run):
        '''Executes a single run of the model.'''
        # Run model
        my_model = Model(run_number=run)
        my_model.run()

        # Get patient-level results, add run number
        patient_results = pd.DataFrame(my_model.results_list)
        patient_results['run'] = run

        # Record trial-level results
        trial_results = {
            'run_number': run,
            'scenario': self.param.scenario_name,
            'arrivals': len(patient_results),
            'mean_q_time_nurse': patient_results['q_time_nurse'].mean(),
            'average_nurse_utilisation': (
                my_model.nurse_time_used /
                (self.param.number_of_nurses * self.param.sim_duration))
        }

        # Collect interval audit results
        interval_audit_df = pd.DataFrame(my_model.utilisation_audit)
        interval_audit_df['run'] = run
        interval_audit_df['perc_utilisation'] = (
            interval_audit_df['number_utilised'] /
            interval_audit_df['number_available']
        )
        return {
            'patient': patient_results,
            'trial': trial_results,
            'interval_audit': interval_audit_df
        }

    def run_trial(self, cores=1):
        '''
        Execute trial with multiple runs
        Default 1 will run sequentially
        -1 means it will use every available core
        Otherwise can specify desired number
        '''
        # Sequential execution
        if cores == 1:
            all_results = [self.run_single(run)
                           for run in range(self.param.number_of_runs)]
        # Parallel execution
        else:
            all_results = Parallel(n_jobs=cores)(
                delayed(self.run_single)(run)
                for run in range(self.param.number_of_runs))

        # Seperate results from each run into appropriate lists
        patient_results_list = [
            result['patient'] for result in all_results]
        trial_results_list = [
            result['trial'] for result in all_results]
        interval_audit_list = [
            result['interval_audit'] for result in all_results]

        # Convert lists into dataframes
        self.patient_results_df = pd.concat(patient_results_list,
                                            ignore_index=True)
        self.trial_results_df = pd.DataFrame(trial_results_list)
        self.interval_audit_df = pd.concat(interval_audit_list,
                                           ignore_index=True)
