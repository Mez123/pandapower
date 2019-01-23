# -*- coding: utf-8 -*-

# Copyright (c) 2016-2018 by University of Kassel and Fraunhofer Institute for Energy Economics
# and Energy System Technology (IEE), Kassel. All rights reserved.
import numpy as np
from scipy.stats import chi2

from pandapower.idx_brch import F_BUS, T_BUS, BR_STATUS, PF, PT, QF, QT
from pandapower.auxiliary import _add_pf_options, get_values, _clean_up
from pandapower.pf.ppci_variables import _get_pf_variables_from_ppci, _store_results_from_pf_in_ppci
from pandapower.pf.pfsoln_pypower import pfsoln
from pandapower.results import _copy_results_ppci_to_ppc, _extract_results_se
from pandapower.topology import estimate_voltage_vector

from pandapower.estimation.ppc_conversions import _add_measurements_to_ppc, \
    _build_measurement_vectors, _init_ppc,\
    _add_aux_elements_for_bb_switch, _drop_aux_elements_for_bb_switch
from pandapower.estimation.results import _copy_power_flow_results, _rename_results
from pandapower.estimation.estimator.wls import WLSEstimator, WLSEstimatorZeroInjectionConstraints
from pandapower.estimation.estimator.robust import QCEstimator



try:
    import pplog as logging
except ImportError:
    import logging
std_logger = logging.getLogger(__name__)

ESTIMATOR_MAPPING = {'wls': WLSEstimator,
                     'wls_with_zero_constraint': WLSEstimatorZeroInjectionConstraints,
                     'qc': QCEstimator}


def initialize_voltage(net, init):
    v_start = None
    delta_start = None
    if init == 'results':
        v_start = net.res_bus_est.vm_pu
        delta_start = net.res_bus_est.va_degree
    elif init == 'slack':
        res_bus = estimate_voltage_vector(net)
        v_start = res_bus.vm_pu.values
        if calculate_voltage_angles:
            delta_start = res_bus.va_degree.values
    elif init != 'flat':
        raise UserWarning("Unsupported init value. Using flat initialization.")
    return v_start, delta_start
    


def estimate(net, algorithm='wls', init='flat', tolerance=1e-6, maximum_iterations=10,
             calculate_voltage_angles=True, zero_injection_detection=False, fuse_all_bb_switches=True,
             **hyperparameter):
    """
    Wrapper function for WLS state estimation.

    INPUT:
        **net** - The net within this line should be created.

        **init** - (string) Initial voltage for the estimation. 'flat' sets 1.0 p.u. / 0° for all
        buses, 'results' uses the values from *res_bus_est* if available and 'slack' considers the
        slack bus voltage (and optionally, angle) as the initial values. Default is 'flat'.

    OPTIONAL:
        **tolerance** - (float) - When the maximum state change between iterations is less than
        tolerance, the process stops. Default is 1e-6.

        **maximum_iterations** - (integer) - Maximum number of iterations. Default is 10.

        **calculate_voltage_angles** - (boolean) - Take into account absolute voltage angles and phase
        shifts in transformers, if init is 'slack'. Default is True.
        
        **zero_injection_detection** - (boolean) - Find out the bus with zero injection (Bus without p,q 
        measurement and p,q injection from elements) and using the wls with constraints estimator to do
        constraint state estimation        
        
        **fuse_all_bb_switches** - (boolean) - if true when considering bus-bus-switches the buses
        will fused (Default behaviour) otherwise auxiliary lines will be added between those buses 
        where an element is connected to them in order to clear the p,q results on each buses 
        instead of fusing them all together

    OUTPUT:
        **successful** (boolean) - Was the state estimation successful?
    """
    if zero_injection_detection:
        algorithm = 'wls_with_zero_constraint'

    if algorithm not in ESTIMATOR_MAPPING:
        raise UserWarning("Algorithm {} is not a valid estimator".format(algorithm))

    wls = StateEstimation(net, tolerance, maximum_iterations, algorithm=algorithm)
    v_start, delta_start = initialize_voltage(net, init)
    return wls.estimate(v_start, delta_start, calculate_voltage_angles, fuse_all_bb_switches, **hyperparameter)


def remove_bad_data(net, init='flat', tolerance=1e-6, maximum_iterations=10,
                    calculate_voltage_angles=True, rn_max_threshold=3.0):
    """
    Wrapper function for bad data removal.

    INPUT:
        **net** - The net within this line should be created.

        **init** - (string) Initial voltage for the estimation. 'flat' sets 1.0 p.u. / 0° for all
        buses, 'results' uses the values from *res_bus_est* if available and 'slack' considers the
        slack bus voltage (and optionally, angle) as the initial values. Default is 'flat'.

    OPTIONAL:
        **tolerance** - (float) - When the maximum state change between iterations is less than
        tolerance, the process stops. Default is 1e-6.

        **maximum_iterations** - (integer) - Maximum number of iterations. Default is 10.

        **calculate_voltage_angles** - (boolean) - Take into account absolute voltage angles and phase
        shifts in transformers, if init is 'slack'. Default is True.

        **rn_max_threshold** (float) - Identification threshold to determine
        if the largest normalized residual reflects a bad measurement
        (default value of 3.0)

        **chi2_prob_false** (float) - probability of error / false alarms
        (default value: 0.05)

    OUTPUT:
        **successful** (boolean) - Was the state estimation successful?
    """
    wls = StateEstimation(net, tolerance, maximum_iterations, algorithm="wls")
    v_start, delta_start = initialize_voltage(net, init)
    return wls.perform_rn_max_test(v_start, delta_start, calculate_voltage_angles,
                                   rn_max_threshold)


def chi2_analysis(net, init='flat', tolerance=1e-6, maximum_iterations=10,
                  calculate_voltage_angles=True, chi2_prob_false=0.05):
    """
    Wrapper function for the chi-squared test.

    INPUT:
        **net** - The net within this line should be created.

        **init** - (string) Initial voltage for the estimation. 'flat' sets 1.0 p.u. / 0° for all
        buses, 'results' uses the values from *res_bus_est* if available and 'slack' considers the
        slack bus voltage (and optionally, angle) as the initial values. Default is 'flat'.

    OPTIONAL:
        **tolerance** - (float) - When the maximum state change between iterations is less than
        tolerance, the process stops. Default is 1e-6.

        **maximum_iterations** - (integer) - Maximum number of iterations. Default is 10.

        **calculate_voltage_angles** - (boolean) - Take into account absolute voltage angles and phase
        shifts in transformers, if init is 'slack'. Default is True.

        **chi2_prob_false** (float) - probability of error / false alarms
        (default value: 0.05)

    OUTPUT:
        **bad_data_detected** (boolean) - Returns true if bad data has been detected
    """
    wls = StateEstimation(net, tolerance, maximum_iterations, algorithm="wls")
    v_start, delta_start = initialize_voltage(net, init)
    return wls.perform_chi2_test(v_start, delta_start, calculate_voltage_angles,
                                 chi2_prob_false)


class StateEstimation(object):
    """
    Any user of the estimation module only needs to use the class state_estimation. It contains all
    relevant functions to control and operator the module. Two functions are used to configure the
    system according to the users needs while one function is used for the actual estimation
    process.
    """
    def __init__(self, net, tolerance=1e-6, maximum_iterations=10, algorithm='wls', logger=None):
        self.logger = logger
        if self.logger is None:
            self.logger = std_logger
            # self.logger.setLevel(logging.DEBUG)
        self.net = net
        self.estimator = ESTIMATOR_MAPPING[algorithm](self.net, tolerance, maximum_iterations, self.logger)

        # variables for chi^2 / rn_max tests
        self.delta = None
        self.bad_data_present = None

    def estimate(self, v_start=None, delta_start=None, calculate_voltage_angles=True, fuse_all_bb_switches=True,
                 **hyperparameter):
        """
        The function estimate is the main function of the module. It takes up to three input
        arguments: v_start, delta_start and calculate_voltage_angles. The first two are the initial
        state variables for the estimation process. Usually they can be initialized in a
        "flat-start" condition: All voltages being 1.0 pu and all voltage angles being 0 degrees.
        In this case, the parameters can be left at their default values (None). If the estimation
        is applied continuously, using the results from the last estimation as the starting
        condition for the current estimation can decrease the  amount of iterations needed to
        estimate the current state. The third parameter defines whether all voltage angles are
        calculated absolutely, including phase shifts from transformers. If only the relative
        differences between buses are required, this parameter can be set to False. Returned is a
        boolean value, which is true after a successful estimation and false otherwise.
        The resulting complex voltage will be written into the pandapower network. The result
        fields are found res_bus_est of the pandapower network.

        INPUT:
            **net** - The net within this line should be created

            **v_start** (np.array, shape=(1,), optional) - Vector with initial values for all
            voltage magnitudes in p.u. (sorted by bus index)

            **delta_start** (np.array, shape=(1,), optional) - Vector with initial values for all
            voltage angles in degrees (sorted by bus index)

        OPTIONAL:
            **calculate_voltage_angles** - (bool) - Take into account absolute voltage angles and
            phase shifts in transformers Default is True.
            
            **fuse_all_bb_switches** - (bool) - if true when considering bus-bus-switches the buses
            will fused (Default behaviour) otherwise auxiliary lines will be added between those buses 
            where an element is connected to them in order to clear the p,q results on each buses 
            instead of fusing them all together

        OUTPUT:
            **successful** (boolean) - True if the estimation process was successful

        Optional estimation variables:
            The bus power injections can be accessed with *se.s_node_powers* and the estimated
            values corresponding to the (noisy) measurement values with *se.hx*. (*hx* denotes h(x))

        EXAMPLE:
            success = estimate(np.array([1.0, 1.0, 1.0]), np.array([0.0, 0.0, 0.0]))

        """
        if self.net is None:
            raise UserWarning("Component was not initialized with a network.")
        
        # change the configuration of the pp net to avoid auto fusing of buses connected
        # through bb switch with elements on each bus if this feature enabled
        if not fuse_all_bb_switches and not self.net.switch.empty:
            _add_aux_elements_for_bb_switch(self.net)
        
        # add initial values for V and delta
        # node voltages
        # V<delta
        if v_start is None:
            v_start = np.ones(self.net.bus.shape[0])
        if delta_start is None:
            delta_start = np.zeros(self.net.bus.shape[0])

        # initialize result tables if not existent
        _copy_power_flow_results(self.net)

        # initialize ppc
        ppc, ppci = _init_ppc(self.net, v_start, delta_start, calculate_voltage_angles)

        # add measurements to ppci structure
        ppci = _add_measurements_to_ppc(self.net, ppci)

        # Finished converting pandapower network to ppci
        # Estimate voltage magnitude and angle with the given estimator
        V = self.estimator.estimate(ppci, **hyperparameter)

        # store results for all elements
        # calculate branch results (in ppc_i)
        baseMVA, bus, gen, branch, ref, pv, pq, _, _, _, ref_gens = _get_pf_variables_from_ppci(ppci)
        Ybus, Yf, Yt = ppci['internal']['Ybus'], ppci['internal']['Yf'], ppci['internal']['Yt']
        ppci['bus'], ppci['gen'], ppci['branch'] = pfsoln(baseMVA, bus, gen, branch, Ybus, Yf, Yt, V, ref, ref_gens)

        # calculate bus power injections
        # TODO: TEST!!
        Sbus = np.multiply(V, np.conj(Ybus * V))
        ppci["bus"][:, 2] = Sbus.real  # saved in per unit
        ppci["bus"][:, 3] = Sbus.imag  # saved in per unit

        # convert to pandapower indices
        ppc = _copy_results_ppci_to_ppc(ppci, ppc, mode="se")

        # extract results from ppc
        _add_pf_options(self.net, tolerance_mva=1e-8, trafo_loading="current",
                        numba=True, ac=True, algorithm='nr', max_iteration="auto")
        # writes res_bus.vm_pu / va_degree and res_line
        _extract_results_se(self.net, ppc)

        # restore backup of previous results
        _rename_results(self.net)

        # additionally, write bus power injection results (these are not written in _extract_results)
        mapping_table = self.net["_pd2ppc_lookups"]["bus"]
        self.net.res_bus_est.p_mw   = - get_values(ppc["bus"][:, 2], self.net.bus.index.values,
                                                 mapping_table)
        self.net.res_bus_est.q_mvar = - get_values(ppc["bus"][:, 3], self.net.bus.index.values,
                                                   mapping_table)
        self.net.res_bus_est.index = self.net.bus.index

        _clean_up(self.net)
        # clear the aux elements and calculation results created for the substitution of bb switches
        if not fuse_all_bb_switches and not self.net.switch.empty:
            _drop_aux_elements_for_bb_switch(self.net)

        # delete results which are not correctly calculated
        for k in list(self.net.keys()):
            if k.startswith("res_") and k.endswith("_est") and \
                    k not in ("res_bus_est", "res_line_est", "res_trafo_est", "res_trafo3w_est"):
                del self.net[k]

        return self.estimator.successful

    def perform_chi2_test(self, v_in_out=None, delta_in_out=None,
                          calculate_voltage_angles=True, chi2_prob_false=0.05):
        """
        The function perform_chi2_test performs a Chi^2 test for bad data and topology error
        detection. The function can be called with the optional input arguments v_in_out and
        delta_in_out. Then, the Chi^2 test is performed after calling the function estimate using
        them as input arguments. It can also be called without these arguments if it is called
        from the same object with which estimate had been called beforehand. Then, the Chi^2 test is
        performed for the states estimated by the funtion estimate and the result, the existence of bad data,
        is given back as a boolean. As a optional argument the probability
        of a false measurement can be provided additionally. For bad data detection, the function
        perform_rn_max_test is more powerful and should be the function of choice. For topology
        error detection, however, perform_chi2_test should be used.

        INPUT:
            **v_in_out** (np.array, shape=(1,), optional) - Vector with initial values for all
            voltage magnitudes in p.u. (sorted by bus index)

            **delta_in_out** (np.array, shape=(1,), optional) - Vector with initial values for all
            voltage angles in degrees (sorted by bus index)

        OPTIONAL:
            **calculate_voltage_angles** - (boolean) - Take into account absolute voltage angles and phase
            shifts in transformers, if init is 'slack'. Default is True.

            **chi2_prob_false** (float) - probability of error / false alarms (standard value: 0.05)

        OUTPUT:
            **successful** (boolean) - True if bad data has been detected

        EXAMPLE:
            perform_chi2_test(np.array([1.0, 1.0, 1.0]), np.array([0.0, 0.0, 0.0]), 0.97)

        """
        # 'flat'-start conditions
        if v_in_out is None:
            v_in_out = np.ones(self.net.bus.shape[0])
        if delta_in_out is None:
            delta_in_out = np.zeros(self.net.bus.shape[0])

        # perform SE
        self.estimate(v_in_out, delta_in_out, calculate_voltage_angles)

        # Performance index J(hx)
        J = np.dot(self.estimator.r.T, np.dot(self.estimator.R_inv, self.estimator.r))

        # Number of measurements
        m = len(self.net.measurement)

        # Number of state variables (the -1 is due to the reference bus)
        n = len(self.estimator.V) + len(self.estimator.delta) - 1

        # Chi^2 test threshold
        test_thresh = chi2.ppf(1 - chi2_prob_false, m - n)

        # Print results
        self.logger.debug("Result of Chi^2 test:")
        self.logger.debug("Number of measurements: %d" % m)
        self.logger.debug("Number of state variables: %d" % n)
        self.logger.debug("Performance index: %.2f" % J)
        self.logger.debug("Chi^2 test threshold: %.2f" % test_thresh)

        if J <= test_thresh:
            self.bad_data_present = False
            self.logger.debug("Chi^2 test passed. No bad data or topology error detected.")
        else:
            self.bad_data_present = True
            self.logger.debug("Chi^2 test failed. Bad data or topology error detected.")

        if (v_in_out is not None) and (delta_in_out is not None):
            return self.bad_data_present


    def perform_rn_max_test(self, v_in_out=None, delta_in_out=None,
                            calculate_voltage_angles=True, rn_max_threshold=3.0):
        """
        The function perform_rn_max_test performs a largest normalized residual test for bad data
        identification and removal. It takes two input arguments: v_in_out and delta_in_out.
        These are the initial state variables for the combined estimation and bad data
        identification and removal process. They can be initialized as described above, e.g.,
        using a "flat" start. In an iterative process, the function performs a state estimation,
        identifies a bad data measurement, removes it from the set of measurements
        (only if the rn_max threshold is violated by the largest residual of all measurements,
        which can be modified), performs the state estimation again,
        and so on and so forth until no further bad data measurements are detected.

        INPUT:
            **v_in_out** (np.array, shape=(1,), optional) - Vector with initial values for all
            voltage magnitudes in p.u. (sorted by bus index)

            **delta_in_out** (np.array, shape=(1,), optional) - Vector with initial values for all
            voltage angles in degrees (sorted by bus index)

        OPTIONAL:
            **calculate_voltage_angles** - (boolean) - Take into account absolute voltage angles and phase
            shifts in transformers, if init is 'slack'. Default is True.

            **rn_max_threshold** (float) - Identification threshold to determine
            if the largest normalized residual reflects a bad measurement
            (standard value of 3.0)

            **chi2_prob_false** (float) - probability of error / false alarms
            (standard value: 0.05)

        OUTPUT:
            **successful** (boolean) - True if all bad data could be removed

        EXAMPLE:
            perform_rn_max_test(np.array([1.0, 1.0, 1.0]), np.array([0.0, 0.0, 0.0]), 5.0, 0.05)

        """
        # 'flat'-start conditions
        if v_in_out is None:
            v_in_out = np.ones(self.net.bus.shape[0])
        if delta_in_out is None:
            delta_in_out = np.zeros(self.net.bus.shape[0])

        num_iterations = 0
        v_in = v_in_out
        delta_in = delta_in_out

        while num_iterations <= 10:
            # Estimate the state with bad data identified in previous iteration
            # removed from set of measurements:
            self.estimate(v_in, delta_in, calculate_voltage_angles)

            # Try to remove the bad data
            try:
                # Error covariance matrix:
                R = np.linalg.inv(self.estimator.R_inv)

                # for future debugging: this line's results have changed with the ppc
                # overhaul in April 2017 after commit 9ae5b8f42f69ae39f8c8cf (which still works)
                # there are differences of < 1e-10 for the Omega entries which cause
                # the function to work far worse. As of now it is unclear if it's just numerical
                # accuracy to blame or an error in the code. a sort in the ppc creation function
                # was removed which caused this issue
                # Covariance matrix of the residuals: \Omega = S*R = R - H*G^(-1)*H^T
                # (S is the sensitivity matrix: r = S*e):
                Omega = R - np.dot(self.estimator.H, np.dot(np.linalg.inv(self.estimator.Gm), self.estimator.Ht))

                # Diagonalize \Omega:
                Omega = np.diag(np.diag(Omega))

                # Compute squareroot (|.| since some -0.0 produced nans):
                Omega = np.sqrt(np.absolute(Omega))

                OmegaInv = np.linalg.inv(Omega)

                # Compute normalized residuals (r^N_i = |r_i|/sqrt{Omega_ii}):
                rN = np.dot(OmegaInv, np.absolute(self.estimator.r))

                if max(rN) <= rn_max_threshold:
                    self.logger.debug("Largest normalized residual test passed. "
                                      "No bad data detected.")
                    return True
                else:
                    self.logger.debug(
                        "Largest normalized residual test failed (%.1f > %.1f)."
                        % (max(rN), rn_max_threshold))

                    # Identify bad data: Determine index corresponding to max(rN):
                    idx_rN = np.argsort(rN, axis=0)[-1]

                    # Determine pandapower index of measurement to be removed:
                    meas_idx = self.estimator.pp_meas_indices[idx_rN]

                    # Remove bad measurement:
                    self.logger.debug("Removing measurement: %s"
                                      % self.net.measurement.loc[meas_idx].values[0])
                    self.net.measurement.drop(meas_idx, inplace=True)
                    self.logger.debug("Bad data removed from the set of measurements.")

            except np.linalg.linalg.LinAlgError:
                self.logger.error("A problem appeared while using the linear algebra methods."
                                  "Check and change the measurement set.")
                return False

            self.logger.debug("rN_max identification threshold: %.2f" % rn_max_threshold)
            num_iterations += 1

        return False
