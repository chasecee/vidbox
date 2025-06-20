"""WiFi management for LOOP."""

import os
import subprocess
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import netifaces

from config.schema import WiFiConfig
from utils.logger import get_logger


class WiFiManager:
    """Manages WiFi connection and hotspot fallback."""
    
    def __init__(self, wifi_config: WiFiConfig):
        """Initialize WiFi manager."""
        self.config = wifi_config
        self.logger = get_logger("wifi")
        
        # State tracking
        self.connected = False
        self.hotspot_active = False
        self.current_ssid = None
        self.ip_address = None
        
        # File paths
        self.wpa_supplicant_conf = Path("/etc/wpa_supplicant/wpa_supplicant.conf")
        self.hostapd_conf = Path("/etc/hostapd/hostapd.conf")
        self.dnsmasq_conf = Path("/etc/dnsmasq.conf")
        
        self.logger.info("WiFi manager initialized")
    
    def scan_networks(self) -> List[Dict[str, str]]:
        """Scan for available WiFi networks."""
        try:
            # Trigger scan
            subprocess.run(['sudo', 'iwlist', 'wlan0', 'scan'], 
                         capture_output=True, timeout=10)
            time.sleep(2)
            
            # Get scan results
            result = subprocess.run(['sudo', 'iwlist', 'wlan0', 'scan'], 
                                  capture_output=True, text=True, timeout=10)
            
            networks = []
            current_network = {}
            
            for line in result.stdout.split('\n'):
                line = line.strip()
                
                if 'Cell' in line and 'Address:' in line:
                    if current_network:
                        networks.append(current_network)
                    current_network = {'bssid': line.split('Address: ')[1]}
                
                elif 'ESSID:' in line:
                    essid = line.split('ESSID:')[1].strip('"')
                    if essid:
                        current_network['ssid'] = essid
                
                elif 'Quality=' in line:
                    try:
                        quality_part = line.split('Quality=')[1].split(' ')[0]
                        if '/' in quality_part:
                            current, max_val = quality_part.split('/')
                            quality = int((int(current) / int(max_val)) * 100)
                            current_network['quality'] = quality
                    except (IndexError, ValueError) as e:
                        # Expected when parsing malformed iwlist output
                        self.logger.debug(f"Failed to parse quality from line: {e}")
                
                elif 'Encryption key:' in line:
                    encrypted = 'on' in line.lower()
                    current_network['encrypted'] = encrypted
            
            if current_network:
                networks.append(current_network)
            
            # Sort by quality (best first)
            networks.sort(key=lambda x: x.get('quality', 0), reverse=True)
            
            self.logger.info(f"Found {len(networks)} WiFi networks")
            return networks
            
        except Exception as e:
            self.logger.error(f"Failed to scan networks: {e}")
            return []
    
    def get_current_network_info(self) -> Dict[str, str]:
        """Get information about current network connection."""
        try:
            # Get current SSID
            result = subprocess.run(['iwgetid', '-r'], 
                                  capture_output=True, text=True)
            current_ssid = result.stdout.strip() if result.returncode == 0 else None
            
            # Get IP address
            ip_address = None
            try:
                if 'wlan0' in netifaces.interfaces():
                    addrs = netifaces.ifaddresses('wlan0')
                    if netifaces.AF_INET in addrs:
                        ip_address = addrs[netifaces.AF_INET][0]['addr']
            except (KeyError, IndexError, OSError) as e:
                # Expected when interface is down or not configured
                self.logger.debug(f"Failed to get IP address: {e}")
            
            # Get signal strength
            signal_strength = None
            try:
                result = subprocess.run(['cat', '/proc/net/wireless'], 
                                      capture_output=True, text=True)
                for line in result.stdout.split('\n'):
                    if 'wlan0' in line:
                        parts = line.split()
                        if len(parts) >= 4:
                            signal_strength = parts[3].rstrip('.')
                        break
            except (subprocess.SubprocessError, FileNotFoundError, IndexError) as e:
                # Expected when wireless interface is unavailable
                self.logger.debug(f"Failed to get signal strength: {e}")
            
            return {
                'ssid': current_ssid,
                'ip_address': ip_address,
                'signal_strength': signal_strength,
                'connected': current_ssid is not None and ip_address is not None
            }
            
        except Exception as e:
            self.logger.error(f"Failed to get network info: {e}")
            return {'connected': False}
    
    def connect_to_network(self, ssid: str, password: str = "") -> bool:
        """Connect to a specific WiFi network."""
        try:
            # First, ensure hotspot is stopped
            if self.hotspot_active:
                self.logger.info("Hotspot is active, stopping it first.")
                if not self.stop_hotspot():
                    self.logger.warning("Failed to stop hotspot, connection may fail.")
                    # Even if stopping fails, we should proceed with connection attempt
            
            self.logger.info(f"Attempting to connect to {ssid}")
            
            # Update wpa_supplicant configuration
            if not self._update_wpa_supplicant(ssid, password):
                return False
            
            # Restart wpa_supplicant
            subprocess.run(['sudo', 'systemctl', 'restart', 'wpa_supplicant'])
            time.sleep(2)
            
            # Try to connect
            subprocess.run(['sudo', 'wpa_cli', '-i', 'wlan0', 'reconfigure'])
            
            # Wait for connection (up to 30 seconds)
            for attempt in range(30):
                time.sleep(1)
                network_info = self.get_current_network_info()
                
                if network_info.get('connected') and network_info.get('ssid') == ssid:
                    self.connected = True
                    self.current_ssid = ssid
                    self.ip_address = network_info.get('ip_address')
                    self.hotspot_active = False
                    
                    # Update config
                    self.config.ssid = ssid
                    self.config.password = password
                    
                    self.logger.info(f"Successfully connected to {ssid} with IP {self.ip_address}")
                    return True
            
            self.logger.warning(f"Failed to connect to {ssid} within timeout")
            return False
            
        except Exception as e:
            self.logger.error(f"Failed to connect to network: {e}")
            return False
    
    def _update_wpa_supplicant(self, ssid: str, password: str) -> bool:
        """Update wpa_supplicant configuration."""
        try:
            # Read current config
            config_lines = []
            if self.wpa_supplicant_conf.exists():
                with open(self.wpa_supplicant_conf, 'r') as f:
                    config_lines = f.readlines()
            
            # Remove existing network blocks for this SSID
            new_lines = []
            in_network_block = False
            network_ssid = None
            
            for line in config_lines:
                if line.strip().startswith('network={'):
                    in_network_block = True
                    network_ssid = None
                elif line.strip() == '}' and in_network_block:
                    in_network_block = False
                    if network_ssid != ssid:
                        # Keep this network block
                        new_lines.extend(current_block)
                        new_lines.append(line)
                    current_block = []
                elif in_network_block:
                    if line.strip().startswith('ssid='):
                        network_ssid = line.split('=')[1].strip().strip('"')
                        current_block = [f'network={{\n']
                        current_block.append(line)
                    else:
                        current_block.append(line)
                else:
                    new_lines.append(line)
            
            # Add new network configuration
            new_lines.append('\nnetwork={\n')
            new_lines.append(f'    ssid="{ssid}"\n')
            
            if password:
                new_lines.append(f'    psk="{password}"\n')
            else:
                new_lines.append('    key_mgmt=NONE\n')
            
            new_lines.append('    priority=1\n')
            new_lines.append('}\n')
            
            # Write updated config
            with open(self.wpa_supplicant_conf, 'w') as f:
                f.writelines(new_lines)
            
            # Set proper permissions
            subprocess.run(['sudo', 'chmod', '600', str(self.wpa_supplicant_conf)])
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to update wpa_supplicant: {e}")
            return False
    
    def connect(self) -> bool:
        """Connect using configured WiFi credentials."""
        if not self.config.ssid:
            self.logger.info("No WiFi SSID configured")
            return False
        
        return self.connect_to_network(self.config.ssid, self.config.password)
    
    def start_hotspot(self) -> bool:
        """Start WiFi hotspot mode."""
        try:
            self.logger.info("Starting WiFi hotspot mode")
            
            # Check if we have write permissions to system files
            import os
            hostapd_dir = os.path.dirname(self.hostapd_conf)
            if not os.access(hostapd_dir, os.W_OK):
                self.logger.warning("Cannot write to hostapd config directory - hotspot may not work properly")
                # Continue anyway - might be running in development
            
            # Try using the helper script first
            try:
                result = subprocess.run([
                    'sudo', str(Path(__file__).parent / 'hotspot.sh'), 
                    'start', 
                    self.config.hotspot_ssid, 
                    self.config.hotspot_password,
                    str(self.config.hotspot_channel)
                ], capture_output=True, text=True, timeout=30)
                
                if result.returncode == 0:
                    self.hotspot_active = True
                    self.connected = False
                    self.current_ssid = None
                    self.ip_address = "192.168.24.1"
                    self.logger.info(f"Hotspot started: {self.config.hotspot_ssid}")
                    return True
                else:
                    self.logger.error(f"Hotspot helper script failed with exit code {result.returncode}")
                    self.logger.error(f"Stderr: {result.stderr.strip()}")
                    return False
            
            except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
                self.logger.error(f"Failed to execute hotspot helper script: {e}")
                return False

        except Exception as e:
            self.logger.error(f"An unexpected error occurred while starting hotspot: {e}")
            return False
    
    def stop_hotspot(self) -> bool:
        """Stop WiFi hotspot mode."""
        try:
            self.logger.info("Stopping WiFi hotspot")

            script_path = str(Path(__file__).parent / 'hotspot.sh')
            result = subprocess.run(
                ['sudo', script_path, 'stop'],
                capture_output=True, text=True, timeout=30
            )

            if result.returncode == 0:
                self.hotspot_active = False
                self.logger.info("Hotspot stopped successfully via script")
                # Attempt to restart dhcpcd to get back on a normal network
                subprocess.run(['sudo', 'systemctl', 'restart', 'dhcpcd'], 
                             capture_output=True, timeout=10)
                return True
            else:
                self.logger.warning(f"Hotspot script failed: {result.stderr}. Falling back to manual stop.")
                # Fallback to original manual method if script fails
                subprocess.run(['sudo', 'systemctl', 'stop', 'hostapd'], check=True)
                subprocess.run(['sudo', 'systemctl', 'stop', 'dnsmasq'], check=True)
                subprocess.run(['sudo', 'ip', 'addr', 'flush', 'dev', 'wlan0'], check=True)
                self.hotspot_active = False
                return False

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as e:
            self.logger.error(f"Failed to stop hotspot: {e}")
            self.hotspot_active = False # Assume it's stopped even on error
            return False
    
    def get_status(self) -> Dict:
        """Get current WiFi status."""
        network_info = self.get_current_network_info()
        
        return {
            'connected': self.connected,
            'hotspot_active': self.hotspot_active,
            'current_ssid': self.current_ssid,
            'ip_address': self.ip_address,
            'configured_ssid': self.config.ssid,
            'hotspot_ssid': self.config.hotspot_ssid,
            'signal_strength': network_info.get('signal_strength'),
            'network_info': network_info
        }
    
    def cleanup(self) -> None:
        """Clean up WiFi manager."""
        if self.hotspot_active:
            self.stop_hotspot()
        
        self.logger.info("WiFi manager cleaned up") 