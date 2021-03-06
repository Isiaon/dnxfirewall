#!/usr/bin/env python3

import os, sys
import time
import json
import threading

from collections import deque
from socket import inet_aton
from ipaddress import IPv4Address, IPv4Network, IPv4Interface

HOME_DIR = os.environ['HOME_DIR']
sys.path.insert(0, HOME_DIR)

from dnx_configure.dnx_constants import * # pylint: disable=unused-wildcard-import
from dnx_iptools.dnx_interface import get_netmask
from dnx_logging.log_main import LogHandler as Log
from dnx_configure.dnx_file_operations import load_configuration, cfg_read_poller, ConfigurationManager
from dnx_iptools.dnx_standard_tools import looper, dnx_queue, Initialize


class Configuration:
    _setup = False

    def __init__(self, name):
        self.initialize = Initialize(Log, name)

    @classmethod
    def setup(cls, DHCPServer):
        if (cls._setup):
            raise RuntimeError('configuration setup should only be called once.')

        cls._setup = True

        self = cls(DHCPServer.__name__)
        self.DHCPServer = DHCPServer

        self._load_interfaces()

        threading.Thread(target=self._get_settings).start()
        threading.Thread(target=self._get_server_options).start()
        threading.Thread(target=self._get_reservations).start()
        self.initialize.wait_for_threads(count=3)

    @cfg_read_poller('dhcp_server')
    def _get_settings(self, cfg_file):
        dhcp_settings = load_configuration(cfg_file)['dhcp_server']

        # TODO: add a change detection check??

        # updating user configuration items per interface in memory.
        for settings in dhcp_settings['interfaces'].values():
            # NOTE: probably temporary. same as below
            if not settings['enabled']: continue

            # NOTE ex. ident: eth0, lo, enp0s3
            intf_identity = settings['ident']

            # identity will be kept in settings just in case, though they key is the identity also.
            self.DHCPServer.intf_settings[intf_identity].update(settings)

        self.initialize.done()

    @cfg_read_poller('dhcp_server')
    def _get_server_options(self, cfg_file):
        dhcp_settings = load_configuration(cfg_file)['dhcp_server']
        server_options = dhcp_settings['options']
        interfaces = dhcp_settings['interfaces']

        # if server options have not changed, the function can return
        if (server_options == self.DHCPServer.options): return

        # will wait for 2 threads to checking before running code. this will allow the necessary settings
        # to be initialized on startup before this thread continues.
        self.initialize.wait_in_line(2)

        with self.DHCPServer.options_lock:
            # iterating over server interfaces and populated server option data sets NOTE: consider merging server
            # options with the interface settings since they are technically bound.
            for intf, settings in self.DHCPServer.intf_settings.items():
                for _intf in interfaces.values():
                    # ensuring the iterfaces match since we cannot guarantee order
                    if (intf != _intf['ident']): continue

                    # NOTE: should be temporary, while dmz is not fully implemented
                    if not settings['enabled']: continue

                    # converting keys to integers (json keys are string only), then packing any
                    # option value that is in ip address form to raw bytes.
                    for o_id, values in server_options.items():
                        opt_len, opt_val = values
                        if (not isinstance(opt_val, str)):
                            self.DHCPServer.options[intf][int(o_id)] = (opt_len, opt_val)

                        else:
                            # NOTE: this is temporary to allow interface netmask to be populated correction while migrating
                            # to new system backend functions.
                            if (o_id == '1'):
                                ip_value = get_netmask(interface=intf)
                            else:
                                ip_value = list(settings['ip'].network)[int(opt_val)]

                            # using digit as ipv4 network object index to grab correct ip object, then pack.
                            self.DHCPServer.options[intf][int(o_id)] = (
                                opt_len, ip_value.packed
                            )

        self.initialize.done()

    # loading user configured dhcp reservations from json config file into memory.
    @cfg_read_poller('dhcp_server')
    def _get_reservations(self, cfg_file):
        dhcp_settings = load_configuration(cfg_file)['dhcp_server']

        self.DHCPServer.reservations = dhcp_settings['reservations']

        reservations = self.DHCPServer.reservations
        reservation_list = list(reservations.items())
        dhcp_leases = list(self.DHCPServer.leases.items())
        for ip, record in dhcp_leases:
            r_type, *_ = record
            ip_object = IPv4Address(ip)
            if (r_type is DHCP.RESERVATION and ip_object not in reservations):
                self.DHCPServer.leases.pop(ip_object)

        # configuring dhcp reservations
        self.DHCPServer.leases.update({
            IPv4Address(ip): (DHCP.RESERVATION, 0, mac) for ip, mac in reservation_list
        })

        self.initialize.done()

    # accessing class object via local instance to change overall DHCP server enabled ints tuple
    def _load_interfaces(self):
        fw_settings     = load_configuration('config')['settings']
        server_settings = load_configuration('dhcp_server')['dhcp_server']
        fw_intf = fw_settings['interfaces']
        dhcp_intfs = server_settings['interfaces']

        # ident
        for intf in self.DHCPServer._intfs:
            # friendly name
            for _intf, settings in dhcp_intfs.items():
                # ensuring the iterfaces match since we cannot guarantee order
                if (intf != settings['ident']): continue

                # passing over disabled server interfaces. NOTE: DEF temporary
                if not dhcp_intfs[_intf]['enabled']: continue

                # creating ipv4 interface object which will be associated with the ident in the config.
                # this can then be used by the server to identify itself as well as generate its effective
                # subnet based on netmask for ip handouts or membership tests.
                intf_ip = IPv4Interface(str(fw_intf[_intf]['ip']) + '/' + str(fw_intf[_intf]['netmask']))

                # initializing server options so the auto loader doesnt have to worry about it.
                self.DHCPServer.options[intf] = {}

                # updating general network information for interfaces on server class object. these will never change
                # while the server is running. for interfaces changes, the server must be restarted.
                self.DHCPServer.intf_settings[intf] = {'ip': intf_ip}


# custom dictionary to manage dhcp server leases including timeouts, updates, or persistence (store to disk)

_NULL_LEASE = (DHCP.AVAILABLE, None, None)


class Leases(dict):
    _setup = False

    __slots__ = (
        '_ip_reservations', '_lease_table_lock'
    )

    def __init__(self, ip_reservations):
        self._ip_reservations = ip_reservations

        self._lease_table_lock = threading.Lock()

        self._load_leases()
        threading.Thread(target=self._lease_table_cleanup).start()
        threading.Thread(target=self._storage).start()

    # if missing will return an expired result
    def __missing__(self, key):
        return _NULL_LEASE

    def modify(self, ip, record):
        '''modifies a record in the lease table. this will automatically ensure changes get written to disk.'''
        # if record is None, the action was a dhcp release so the record will be locally assigned as available.
        if not record:
            record = (DHCP.AVAILABLE, 0, 0)

        # this lock is protecting the internal lease table dict from getting mutated while the cleanup thread is active.
        with self._lease_table_lock:
            self[ip] = record

        # added change to disk storage queue for lease persistence across device/process shutdowns.
        # will only store leases. offers will be treated as volitile and not persist restarts
        if (record[0] is DHCP.LEASED):
            self._storage.add((f'{ip}', record)) # pylint: disable=no-member

    @dnx_queue(Log, name='Leases')
    # store leases table changes to disk. if record is not present that indicates the record needs to be removed.
    def _storage(self, dhcp_lease):
        with ConfigurationManager('dhcp_server') as dnx:
            dhcp_settings = dnx.load_configuration()
            leases = dhcp_settings['dhcp_server']['leases']

            ip, record = dhcp_lease
            if (not record):
                leases.pop(ip, None)
            else:
                leases[ip] = record

            dnx.write_configuration(dhcp_settings)

    # loading dhcp leases from json file. will be called on startup only for lease persistence.
    def _load_leases(self):
        # print('[+] Loading leases from file.')
        dhcp_settings = load_configuration('dhcp_server')

        stored_leases = dhcp_settings['dhcp_server']['leases']
        self.update({
            IPv4Address(ip): lease_info for ip, lease_info in stored_leases.items()
        })

    @looper(ONE_MIN)
    # TODO: TEST RESERVATIONS GET CLEANED UP
    # removing expired entries from lease table on a per interface basis. checked every 1 minutes.
    def _lease_table_cleanup(self):
        current_time = fast_time()
        # copying dictionary as list for local reference performance
        leases_copy = list(self.items())
        with self._lease_table_lock:
            for ip_address, lease in leases_copy:
                lease_type, lease_time, lease_mac = lease
                if (lease_type in [DHCP.AVAILABLE]): continue

                time_elapsed = current_time - lease_time
                # ip reservation has been removed from system
                if (lease_type == DHCP.RESERVATION and lease_mac not in self._ip_reservations):
                    self[ip_address] = (DHCP.AVAILABLE, 0, 0)

                # client did not accept our ip offer
                elif (lease_type == DHCP.OFFERED and time_elapsed > ONE_MIN):
                    self[ip_address] = (DHCP.AVAILABLE, 0, 0)

                # ip lease expired normally
                # NOTE: consider moving this value to a global constant/ make configurable
                elif (time_elapsed >= 86800):
                    self[ip_address] = (DHCP.AVAILABLE, 0, 0)

                # unknown condition? maybe log?
                else: continue

                self._storage.add((ip_address, None)) # pylint: disable=no-member
