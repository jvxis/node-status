import configparser
from flask import Flask, render_template, request, jsonify
import subprocess
import json
import requests
import psutil
import cpuinfo
import sensors
from collections import defaultdict
import markdown
import os


# Load configuration from external file
config = configparser.ConfigParser()
config.read('node-status.config')

# Configuration settings
RUNNING_ENVIRONMENT = config.get('settings', 'RUNNING_ENVIRONMENT', fallback='minibolt')
RUNNING_BITCOIN = config.get('settings', 'RUNNING_BITCOIN', fallback='external')

# Only if you're running Bitcoin Core on another machine
BITCOIN_RPC_USER = config.get('bitcoin', 'BITCOIN_RPC_USER', fallback='YOUR_BITCOIN_RPCUSER')
BITCOIN_RPC_PASSWORD = config.get('bitcoin', 'BITCOIN_RPC_PASSWORD', fallback='YOUR_BITCOIN_RPCPASS')
BITCOIN_RPC_HOST = config.get('bitcoin', 'BITCOIN_RPC_HOST', fallback='YOUR_BITCOIN_MACHINE_IP')
BITCOIN_RPC_PORT = config.get('bitcoin', 'BITCOIN_RPC_PORT', fallback='8332')

# For Umbrel Users
UMBREL_PATH = config.get('umbrel', 'UMBREL_PATH', fallback='/path/to/umbrel/scripts/')

# Message to display
MESSAGE_FILE_PATH = config.get('settings', 'MESSAGE_FILE_PATH', fallback='/home/admin/node-status/templates/message.txt')


app = Flask(__name__)

def run_command(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {command}\n{result.stderr}")
    return result.stdout

def get_bitcoin_info():
    if RUNNING_ENVIRONMENT == 'minibolt' and RUNNING_BITCOIN == 'external':
        rpc_host = BITCOIN_RPC_HOST  # Use the global configuration
        bitcoin_cli_base_cmd = [
            'bitcoin-cli',
            f'-rpcuser={BITCOIN_RPC_USER}',
            f'-rpcpassword={BITCOIN_RPC_PASSWORD}',
            f'-rpcconnect={rpc_host}',
            f'-rpcport={BITCOIN_RPC_PORT}'
        ]
        blockchain_info_cmd = bitcoin_cli_base_cmd + ['getblockchaininfo']
        peers_info_cmd = bitcoin_cli_base_cmd + ['getpeerinfo']
        network_info_cmd = bitcoin_cli_base_cmd + ['getnetworkinfo']
        bitcoind_host_display = BITCOIN_RPC_HOST  # Usando o valor do arquivo de configuração
    elif RUNNING_ENVIRONMENT == 'minibolt' and RUNNING_BITCOIN == 'local':
        bitcoin_cli_base_cmd = ['bitcoin-cli']
        blockchain_info_cmd = bitcoin_cli_base_cmd + ['getblockchaininfo']
        peers_info_cmd = bitcoin_cli_base_cmd + ['getpeerinfo']
        network_info_cmd = bitcoin_cli_base_cmd + ['getnetworkinfo']
        rpc_host = 'LOCAL - Minibolt'
    else:  # umbrel
        rpc_host = 'LOCAL - Umbrel'
        blockchain_info_cmd = [
            f"{UMBREL_PATH}app", "compose", "bitcoin", "exec",
            "bitcoind", "bitcoin-cli", 'getblockchaininfo'
        ]
        peers_info_cmd = [
            f"{UMBREL_PATH}app", "compose", "bitcoin", "exec",
            "bitcoind", "bitcoin-cli", 'getpeerinfo'
        ]
        network_info_cmd = [
            f"{UMBREL_PATH}app", "compose", "bitcoin", "exec",
            "bitcoind", "bitcoin-cli", 'getnetworkinfo'
        ]

    # Execute the commands and parse JSON output
    blockchain_data = json.loads(run_command(blockchain_info_cmd))
    peers_data = json.loads(run_command(peers_info_cmd))
    network_data = json.loads(run_command(network_info_cmd))

    return {
        "sync_percentage": blockchain_data.get("verificationprogress", 0) * 100,
        "current_block_height": blockchain_data.get("blocks", 0),
        "chain": blockchain_data.get("chain", "unknown"),
        "pruned": blockchain_data.get("pruned", False),
        "number_of_peers": len(peers_data),
        "bitcoind": rpc_host,
        "version": network_data.get("version", "unknown"),
        "subversion": network_data.get("subversion", "unknown")
    }


def get_lnd_info():
    if RUNNING_ENVIRONMENT == 'minibolt':
        lncli_cmd = ['lncli']
    else:  # umbrel
        lncli_cmd = [f"{UMBREL_PATH}app", "compose", "lightning", "exec", "lnd", "lncli"]

    wallet_balance_data = json.loads(run_command(lncli_cmd + ['walletbalance']))
    channel_balance_data = json.loads(run_command(lncli_cmd + ['channelbalance']))
    channels_data = json.loads(run_command(lncli_cmd + ['listchannels']))
    peers_data = json.loads(run_command(lncli_cmd + ['listpeers']))
    node_data = json.loads(run_command(lncli_cmd + ['getinfo']))

    return {
        "wallet_balance": int(wallet_balance_data["total_balance"]),
        "channel_balance": int(channel_balance_data["balance"]),
        "total_balance": int(wallet_balance_data["total_balance"]) + int(channel_balance_data["balance"]),
        "number_of_channels": len(channels_data["channels"]),
        "number_of_peers": len(peers_data["peers"]),
        "node_alias": node_data["alias"],
        "node_lnd_version": node_data["version"],
        "pub_key": node_data["identity_pubkey"],
        "num_pending_channels": node_data["num_pending_channels"],
        "num_active_channels": node_data["num_active_channels"],
        "num_inactive_channels": node_data["num_inactive_channels"],
        "synced_to_chain": node_data["synced_to_chain"],
        "synced_to_graph": node_data["synced_to_graph"]
    }

def read_message_from_file():
    try:
        with open(MESSAGE_FILE_PATH, 'r') as file:
            message = file.read().strip()
        # Convert the message from Markdown to HTML
        message_html = markdown.markdown(message)
        return message_html
    except FileNotFoundError:
        return "No message found."

def get_fee_info():
    response = requests.get("https://mempool.space/api/v1/fees/recommended")
    return response.json()

def get_cpu_usage():
    return psutil.cpu_percent(interval=1)

def get_memory_usage():
    return psutil.virtual_memory().percent

def get_cpu_info():
    return cpuinfo.get_cpu_info()

def get_physical_disks_usage():
    disk_usage = defaultdict(lambda: {'total': 0, 'used': 0, 'free': 0})
    for partition in psutil.disk_partitions(all=False):
        if 'loop' not in partition.device and 'ram' not in partition.device:
            usage = psutil.disk_usage(partition.mountpoint)
            device = partition.device.split('p')[0]
            disk_usage[device]['total'] += usage.total
            disk_usage[device]['used'] += usage.used
            disk_usage[device]['free'] += usage.free

    for device, usage in disk_usage.items():
        usage['percent'] = (usage['used'] / usage['total']) * 100

    return disk_usage

def get_cpu_temp():
    temps = psutil.sensors_temperatures()
    if 'coretemp' in temps:
        for entry in temps['coretemp']:
            if entry.label == 'Package id 0':
                return entry.current
    return 0

def get_sensor_temperatures():
    sensors.init()
    sensor_temps = []
    try:
        for chip in sensors.iter_detected_chips():
            try:
                chip_name = str(chip)
            except sensors.SensorsError as e:
                chip_name = f"Unknown chip ({e})"
            for feature in chip:
                if "composite" in feature.label.lower():
                    try:
                        feature_label = feature.label
                        feature_value = feature.get_value()
                        sensor_temps.append((chip_name, feature_label, feature_value))
                    except sensors.SensorsError as e:
                        sensor_temps.append((chip_name, "Unknown feature", f"Error reading feature: {e}"))
    finally:
        sensors.cleanup()
    return sensor_temps


# Route to fetch the latest lines from the log file
@app.route('/get-log', methods=['GET'])
def get_log():
    log_path = os.path.expanduser("~/.lnd/logs/bitcoin/mainnet/lnd.log")
    
    # Check if the log file exists
    if not os.path.isfile(log_path):
        return jsonify({"error": "Log file not found"}), 404

    # Get the number of lines to fetch (default to 20)
    lines = request.args.get('lines', default=20, type=int)

    try:
        # Fetch the latest N lines from the log file using 'tail'
        result = subprocess.run(['tail', '-n', str(lines), log_path], capture_output=True, text=True)
        return jsonify({"logs": result.stdout})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/decode-invoice', methods=['POST'])
def decode_invoice():
    data = request.get_json()
    pay_req = data.get('pay_req')
    if not pay_req:
        return jsonify({'error': 'Missing payment request'}), 400

    try:
        result = run_command(['lncli', 'decodepayreq', pay_req])
        decoded_data = json.loads(result)
        return jsonify({
            'amount': decoded_data.get('num_satoshis', 'N/A'),
            'message': decoded_data.get('description', 'No message')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/pay-invoice', methods=['POST'])
def pay_invoice():
    data = request.get_json()
    pay_req = data.get('pay_req')
    if not pay_req:
        return jsonify({'error': 'Missing payment request'}), 400

    try:
        result = run_command(['lncli', 'payinvoice', '--force', pay_req])
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e), 'success': False}), 500
    
@app.route('/generate-invoice', methods=['POST'])
def generate_invoice():
    data = request.get_json()
    amount = data.get('amount', 0)
    message = data.get('message', '')  # Ensure message (memo) is properly captured

    print(f"Received amount: {amount}, memo: {message}")  # Log the received values

    if not amount or int(amount) <= 0:
        return jsonify({'error': 'Amount must be greater than 0.'}), 400

    try:
        cmd = ['lncli', 'addinvoice', '--amt', str(amount), '--memo', message, '--expiry', '600']
        result = json.loads(run_command(cmd))

        return jsonify({
            'r_hash': result['r_hash'],
            'payment_request': result['payment_request']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500




@app.route('/check-payment', methods=['GET'])
def check_payment():
    r_hash = request.args.get('r_hash')

    if not r_hash:
        return jsonify({'error': 'Missing r_hash'}), 400

    try:
        # Correct the flag to --rhash
        cmd = ['lncli', 'lookupinvoice', '--rhash', r_hash]
        result = json.loads(run_command(cmd))  # Use your existing function to execute the command

        # Log the result for debugging
        print(f"Lookupinvoice result: {result}")

        # Return the relevant data from the result
        return jsonify({
            'settled': result.get('settled', False),
            'amount': result.get('amt_paid_sat', 0),
            'message': result.get('memo', '')
        })
    except Exception as e:
        # Log the exception for debugging purposes
        print(f"Error looking up invoice with r_hash {r_hash}: {e}")
        return jsonify({'error': str(e)}), 500



@app.route('/status')
def status():
    system_info = {
        "cpu_usage": get_cpu_usage(),
        "memory_usage": get_memory_usage(),
        "cpu_info": get_cpu_info(),
        "cpu_temp": get_cpu_temp(),
        "physical_disks_usage": get_physical_disks_usage(),
        "sensor_temperatures": get_sensor_temperatures()
    }
    bitcoin_info = get_bitcoin_info()
    lnd_info = get_lnd_info()
    message = read_message_from_file()
    fee_info = get_fee_info()
    return render_template('status.html', system_info=system_info, bitcoind=bitcoin_info, lnd=lnd_info, node_alias=lnd_info["node_alias"], message=message, fee_info=fee_info)

    

if __name__ == '__main__':
    #For self-signed uncomment the line below. This will be need to use the mobile camera
    
    app.run(host='0.0.0.0', port=5000, ssl_context=('cert-ns.pem', 'key-ns.pem'))
    
    #!!!Don't forget to comment the line below if you uncomment the line above!!!
    #app.run(host='0.0.0.0', port=5000)import configparser
