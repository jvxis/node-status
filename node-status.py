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
import shutil
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# para usar o viewer do lnd_fees.sqlite (jvx)
from lnd_fees_view import fetch_daily_latest, fetch_month_summary, fetch_ytd

# -----------------------------
# Config
# -----------------------------
config = configparser.ConfigParser()
# lê a partir do diretório atual (WorkingDirectory deve ser /home/admin/node-status)
config.read(['node-status.config', 'node-status.local.config'])

RUNNING_ENVIRONMENT = config.get('settings', 'RUNNING_ENVIRONMENT', fallback='minibolt')
RUNNING_BITCOIN     = config.get('settings', 'RUNNING_BITCOIN',     fallback='external')

# Se Bitcoin Core for externo, preencha no .config
BITCOIN_RPC_USER = config.get('bitcoin', 'BITCOIN_RPC_USER',    fallback='YOUR_BITCOIN_RPCUSER')
BITCOIN_RPC_PASS = config.get('bitcoin', 'BITCOIN_RPC_PASSWORD',fallback='YOUR_BITCOIN_RPCPASS')
BITCOIN_RPC_HOST = config.get('bitcoin', 'BITCOIN_RPC_HOST',    fallback='YOUR_BITCOIN_MACHINE_IP')
BITCOIN_RPC_PORT = config.get('bitcoin', 'BITCOIN_RPC_PORT',    fallback='8332')

# Umbrel
UMBREL_PATH = config.get('umbrel', 'UMBREL_PATH', fallback='/path/to/umbrel/scripts/')

# Mensagem custom
MESSAGE_FILE_PATH = config.get('settings', 'MESSAGE_FILE_PATH', fallback='/home/admin/node-status/templates/message.txt')

app = Flask(__name__)

# -----------------------------
# Helpers robustos
# -----------------------------
def run_command(command, timeout=5):
    """
    Executa um comando CLI com timeout.
    Lança RuntimeError com mensagem clara em caso de falha.
    """
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(command)} -> exit {result.returncode}: {result.stderr.strip()}")
        return result.stdout
    except Exception as e:
        raise RuntimeError(str(e))

def read_message_from_file():
    try:
        with open(MESSAGE_FILE_PATH, 'r') as file:
            message = file.read().strip()
        # Markdown -> HTML
        return markdown.markdown(message)
    except FileNotFoundError:
        return "No message found."
    except Exception as e:
        return f"Message error: {e}"

def _http_json(url, timeout=5):
    """GET JSON com requests; retorna dict ou lança."""
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "node-status/1.0"})
    r.raise_for_status()
    return r.json()

def _torsocks_curl_json(url, timeout=10):
    """GET via torsocks+curl; retorna dict ou lança."""
    if not shutil.which("torsocks") or not shutil.which("curl"):
        raise RuntimeError("torsocks/curl indisponível")
    out = subprocess.run(
        ["torsocks", "curl", "-s", "--max-time", str(timeout), url],
        capture_output=True, text=True, timeout=timeout+2
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"torsocks curl falhou: {out.stderr.strip()}")
    return json.loads(out.stdout)

def get_fee_info():
    """
    Busca taxas com estratégia em cascata:
      1) mempool.space
      2) mirror emzy.de
      3) torsocks+curl para mempool.space
      4) fallback estático
    Retorna dict com as chaves de fee + "_source".
    """
    try:
        d = _http_json("https://mempool.space/api/v1/fees/recommended", timeout=5)
        d["_source"] = "mempool.space"
        return d
    except Exception:
        pass
    try:
        d = _http_json("https://mempool.emzy.de/api/v1/fees/recommended", timeout=5)
        d["_source"] = "mempool.emzy.de"
        return d
    except Exception:
        pass
    try:
        d = _torsocks_curl_json("https://mempool.space/api/v1/fees/recommended", timeout=10)
        d["_source"] = "mempool.space via Tor"
        return d
    except Exception:
        return {
            "fastestFee": 30,
            "halfHourFee": 20,
            "hourFee": 10,
            "economyFee": 5,
            "minimumFee": 1,
            "_source": "fallback"
        }

def get_cpu_usage():
    # não bloquear 1s por request
    return psutil.cpu_percent(interval=0.0)

def get_memory_usage():
    try:
        return psutil.virtual_memory().percent
    except Exception:
        return None

def get_cpu_info():
    try:
        return cpuinfo.get_cpu_info()
    except Exception:
        return {"error": "cpuinfo failed"}

def get_physical_disks_usage():
    """
    Consolida uso por dispositivo físico. Ignora loop/ram.
    Nunca lança exceção; retorna estrutura com percent calculado.
    """
    try:
        disk_usage = defaultdict(lambda: {'total': 0, 'used': 0, 'free': 0})
        for part in psutil.disk_partitions(all=False):
            dev = getattr(part, "device", "") or ""
            if not dev or 'loop' in dev or 'ram' in dev:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            device_key = dev.split('p')[0] if 'p' in dev else dev
            disk_usage[device_key]['total'] += usage.total
            disk_usage[device_key]['used']  += usage.used
            disk_usage[device_key]['free']  += usage.free

        for device, usage in disk_usage.items():
            total = usage['total'] or 0
            usage['percent'] = (usage['used'] / total * 100) if total else 0.0

        return disk_usage
    except Exception:
        return {}

def get_cpu_temp():
    """
    Compatível Intel/AMD; nunca lança exceção.
    Tenta coretemp/k10temp e labels Package id 0/Tctl/Tdie.
    """
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False) or {}
    except Exception:
        return None

    chip = None
    for name in ("coretemp", "k10temp"):
        if name in temps:
            chip = name
            break
    if chip is None and temps:
        chip = next(iter(temps))  # primeiro disponível
    if not chip:
        return None

    entries = temps.get(chip, [])
    for pref in ("Package id 0", "Tctl", "Tdie"):
        for e in entries:
            if (getattr(e, "label", "") or "").strip() == pref:
                return e.current
    return entries[0].current if entries else None

def get_sensor_temperatures():
    """
    Lê sensores via pysensors.
    Nunca derruba a rota; retorna lista de tuplas (chip, label, value) ou erro.
    """
    try:
        sensors.init()
        sensor_temps = []
        for chip in sensors.iter_detected_chips():
            chip_name = str(chip)
            for feature in chip:
                label = getattr(feature, "label", "") or ""
                if "composite" in label.lower():
                    try:
                        sensor_temps.append((chip_name, label, feature.get_value()))
                    except Exception as e:
                        sensor_temps.append((chip_name, label or "Unknown", f"Error: {e}"))
        return sensor_temps
    except Exception as e:
        return [("sensors", "error", str(e))]
    finally:
        try:
            sensors.cleanup()
        except Exception:
            pass

# -----------------------------
# Bitcoin/LND info (com try/except + timeouts)
# -----------------------------
def get_bitcoin_info():
    try:
        if RUNNING_ENVIRONMENT == 'minibolt' and RUNNING_BITCOIN == 'external':
            rpc_host = BITCOIN_RPC_HOST
            bitcoin_cli_base_cmd = [
                'bitcoin-cli',
                f'-rpcuser={BITCOIN_RPC_USER}',
                f'-rpcpassword={BITCOIN_RPC_PASS}',
                f'-rpcconnect={rpc_host}',
                f'-rpcport={BITCOIN_RPC_PORT}'
            ]
            blockchain_info_cmd = bitcoin_cli_base_cmd + ['getblockchaininfo']
            peers_info_cmd      = bitcoin_cli_base_cmd + ['getpeerinfo']
            network_info_cmd    = bitcoin_cli_base_cmd + ['getnetworkinfo']
        elif RUNNING_ENVIRONMENT == 'minibolt' and RUNNING_BITCOIN == 'local':
            rpc_host = 'LOCAL - Minibolt'
            bitcoin_cli_base_cmd = ['bitcoin-cli']
            blockchain_info_cmd = bitcoin_cli_base_cmd + ['getblockchaininfo']
            peers_info_cmd      = bitcoin_cli_base_cmd + ['getpeerinfo']
            network_info_cmd    = bitcoin_cli_base_cmd + ['getnetworkinfo']
        else:  # umbrel
            rpc_host = 'LOCAL - Umbrel'
            blockchain_info_cmd = [f"{UMBREL_PATH}app", "compose", "bitcoin", "exec", "bitcoind", "bitcoin-cli", 'getblockchaininfo']
            peers_info_cmd      = [f"{UMBREL_PATH}app", "compose", "bitcoin", "exec", "bitcoind", "bitcoin-cli", 'getpeerinfo']
            network_info_cmd    = [f"{UMBREL_PATH}app", "compose", "bitcoin", "exec", "bitcoind", "bitcoin-cli", 'getnetworkinfo']

        blockchain_data = json.loads(run_command(blockchain_info_cmd, timeout=4))
        peers_data      = json.loads(run_command(peers_info_cmd,      timeout=4))
        network_data    = json.loads(run_command(network_info_cmd,    timeout=4))

        return {
            "sync_percentage": blockchain_data.get("verificationprogress", 0) * 100,
            "current_block_height": blockchain_data.get("blocks", 0),
            "chain": blockchain_data.get("chain", "unknown"),
            "pruned": blockchain_data.get("pruned", False),
            "number_of_peers": len(peers_data),
            "bitcoind": rpc_host,
            "version": network_data.get("version", "unknown"),
            "subversion": network_data.get("subversion", "unknown"),
            "error": None
        }
    except Exception as e:
        return {
            "sync_percentage": None,
            "current_block_height": None,
            "chain": None,
            "pruned": None,
            "number_of_peers": None,
            "bitcoind": f"error: {e}",
            "version": None,
            "subversion": None,
            "error": str(e)
        }

def get_lnd_info():
    try:
        if RUNNING_ENVIRONMENT == 'minibolt':
            lncli_cmd = ['lncli']
        else:
            lncli_cmd = [f"{UMBREL_PATH}app", "compose", "lightning", "exec", "lnd", "lncli"]

        wallet_balance_data  = json.loads(run_command(lncli_cmd + ['walletbalance'],  timeout=4))
        channel_balance_data = json.loads(run_command(lncli_cmd + ['channelbalance'], timeout=4))
        channels_data        = json.loads(run_command(lncli_cmd + ['listchannels'],   timeout=4))
        peers_data           = json.loads(run_command(lncli_cmd + ['listpeers'],      timeout=4))
        node_data            = json.loads(run_command(lncli_cmd + ['getinfo'],        timeout=4))

        return {
            "wallet_balance": int(wallet_balance_data.get("total_balance", 0)),
            "channel_balance": int(channel_balance_data.get("balance", 0)),
            "total_balance": int(wallet_balance_data.get("total_balance", 0)) + int(channel_balance_data.get("balance", 0)),
            "number_of_channels": len(channels_data.get("channels", [])),
            "number_of_peers": len(peers_data.get("peers", [])),
            "node_alias": node_data.get("alias", "N/A"),
            "node_lnd_version": node_data.get("version"),
            "pub_key": node_data.get("identity_pubkey"),
            "num_pending_channels": node_data.get("num_pending_channels"),
            "num_active_channels": node_data.get("num_active_channels"),
            "num_inactive_channels": node_data.get("num_inactive_channels"),
            "synced_to_chain": node_data.get("synced_to_chain"),
            "synced_to_graph": node_data.get("synced_to_graph"),
            "error": None
        }
    except Exception as e:
        return {
            "wallet_balance": None,
            "channel_balance": None,
            "total_balance": None,
            "number_of_channels": None,
            "number_of_peers": None,
            "node_alias": "N/A",
            "node_lnd_version": None,
            "pub_key": None,
            "num_pending_channels": None,
            "num_active_channels": None,
            "num_inactive_channels": None,
            "synced_to_chain": None,
            "synced_to_graph": None,
            "error": str(e)
        }

# -----------------------------
# Top peers (via lncli fwdinghistory)
# -----------------------------
def get_top_forwarding_peers(days=30, limit=5):
    """
    Calcula top/bottom peers por fees recebidas nos últimos `days` dias.
    Usa somente forwardinghistory do LND (não inclui custo de rebalances).

    Agrupamento por peer_alias_out (alias do peer de saída).
    Isso evita depender de listchannels/scid e funciona mesmo com canais fechados.

    Retorno:
    {
      "window_days": 30,
      "generated_at": "...Z",
      "total_events": 1524,
      "top": [
        {
          "alias": "BCash_Is_Trash",
          "fees_sat": 12345,
          "amount_sat": 9876543,
          "events": 200
        },
        ...
      ],
      "low": [ ... ]
    }
    """
    try:
        # 1) Comando base para lncli
        if RUNNING_ENVIRONMENT == 'minibolt':
            lncli_cmd = ['lncli']
        else:
            lncli_cmd = [f"{UMBREL_PATH}app", "compose", "lightning", "exec", "lnd", "lncli"]

        # 2) Janela de tempo em epoch (UTC)
        now_ts = int(datetime.utcnow().timestamp())
        start_ts = now_ts - days * 86400

        # 3) Histórico de encaminhamentos
        fh_raw = run_command(
            lncli_cmd
            + [
                'fwdinghistory',
                f'--start_time={start_ts}',
                f'--end_time={now_ts}',
                '--max_events=100000',
            ],
            timeout=20,
        )
        fh = json.loads(fh_raw)
        events = fh.get("forwarding_events", []) or []
        total_events = len(events)

        if total_events == 0:
            return {
                "window_days": days,
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_events": 0,
                "top": [],
                "low": [],
            }

        # 4) Agrega por alias de saída (peer_alias_out)
        peers = {}
        for ev in events:
            alias_raw = (ev.get("peer_alias_out") or "").strip()

            # Ignora peers sem alias útil ou com mensagem de erro do LND
            if not alias_raw:
                continue
            lowered = alias_raw.lower()
            if lowered.startswith("unable to lookup peeralias"):
                continue
            if lowered in ("unknown", "unnamed"):
                continue

            alias = alias_raw
            fee_msat = int(ev.get("fee_msat", "0") or "0")
            amt_out_msat = int(ev.get("amt_out_msat", ev.get("amt_out", "0")) or "0")

            p = peers.setdefault(
                alias,
                {
                    "fees_msat": 0,
                    "amt_out_msat": 0,
                    "events": 0,
                },
            )
            p["fees_msat"] += fee_msat
            p["amt_out_msat"] += amt_out_msat
            p["events"] += 1

        # 5) Converte para lista, filtra quem tem fee > 0 e ordena
        items = []
        for alias, v in peers.items():
            items.append(
                {
                    "alias": alias,
                    "fees_sat": v["fees_msat"] // 1000,
                    "amount_sat": v["amt_out_msat"] // 1000,
                    "events": v["events"],
                }
            )

        positive = [p for p in items if p["fees_sat"] > 0]
        if not positive:
            return {
                "window_days": days,
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "total_events": total_events,
                "top": [],
                "low": [],
            }

        positive_sorted = sorted(positive, key=lambda x: x["fees_sat"], reverse=True)
        top = positive_sorted[:limit]
        low = list(reversed(positive_sorted))[:limit]

        return {
            "window_days": days,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_events": total_events,
            "top": top,
            "low": low,
        }
    except Exception as e:
        return {"error": str(e)}

# -----------------------------
# Rotas
# -----------------------------
@app.route('/get-log', methods=['GET'])
def get_log():
    log_path = os.path.expanduser("~/.lnd/logs/bitcoin/mainnet/lnd.log")
    if not os.path.isfile(log_path):
        return jsonify({"error": "Log file not found"}), 404

    lines = request.args.get('lines', default=20, type=int)
    try:
        result = subprocess.run(['tail', '-n', str(lines), log_path], capture_output=True, text=True)
        return jsonify({"logs": result.stdout})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/decode-invoice', methods=['POST'])
def decode_invoice():
    data = request.get_json(silent=True) or {}
    pay_req = data.get('pay_req')
    if not pay_req:
        return jsonify({'error': 'Missing payment request'}), 400
    try:
        result = run_command(['lncli', 'decodepayreq', pay_req], timeout=5)
        decoded = json.loads(result)
        return jsonify({
            'amount':  decoded.get('num_satoshis', 'N/A'),
            'message': decoded.get('description', 'No message')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/pay-invoice', methods=['POST'])
def pay_invoice():
    data = request.get_json(silent=True) or {}
    pay_req = data.get('pay_req')
    if not pay_req:
        return jsonify({'error': 'Missing payment request'}), 400
    try:
        _ = run_command(['lncli', 'payinvoice', '--force', pay_req], timeout=10)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/generate-invoice', methods=['POST'])
def generate_invoice():
    data = request.get_json(silent=True) or {}
    amount  = data.get('amount', 0)
    message = data.get('message', '')
    if not amount or int(amount) <= 0:
        return jsonify({'error': 'Amount must be greater than 0.'}), 400
    try:
        cmd = ['lncli', 'addinvoice', '--amt', str(amount), '--memo', message, '--expiry', '600']
        result = json.loads(run_command(cmd, timeout=5))
        return jsonify({
            'r_hash': result.get('r_hash'),
            'payment_request': result.get('payment_request')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/check-payment', methods=['GET'])
def check_payment():
    r_hash = request.args.get('r_hash')
    if not r_hash:
        return jsonify({'error': 'Missing r_hash'}), 400
    try:
        res = json.loads(run_command(['lncli', 'lookupinvoice', '--rhash', r_hash], timeout=5))
        return jsonify({
            'settled': res.get('settled', False),
            'amount':  res.get('amt_paid_sat', 0),
            'message': res.get('memo', '')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/status')
def status():
    system_info = {
        "cpu_usage":             get_cpu_usage(),
        "memory_usage":          get_memory_usage(),
        "cpu_info":              get_cpu_info(),
        "cpu_temp":              get_cpu_temp(),
        "physical_disks_usage":  get_physical_disks_usage(),
        "sensor_temperatures":   get_sensor_temperatures()
    }
    bitcoin_info = get_bitcoin_info()
    lnd_info     = get_lnd_info()
    message      = read_message_from_file()
    fee_info     = get_fee_info()

    node_alias = lnd_info.get("node_alias", "N/A") if isinstance(lnd_info, dict) else "N/A"

    return render_template(
        'status.html',
        system_info=system_info,
        bitcoind=bitcoin_info,
        lnd=lnd_info,
        node_alias=node_alias,
        message=message,
        fee_info=fee_info,
    )

@app.route("/lnd-fees")
def api_lnd_fees():
    """
    Exposição HTTP da base lnd_fees.sqlite (script do jvx).
    Mantém o formato que o front espera: last_day / monthly / year_to_date.
    Agora também inclui label/date_br para não rotular errado quando o DB estiver atrasado ou em outro timezone.
    """
    try:
        latest = fetch_daily_latest()
        months = fetch_month_summary()
        ytd = fetch_ytd()

        # timezone local (ajuste se quiser outro)
        TZ = ZoneInfo("America/Sao_Paulo")
        today_local = datetime.now(TZ).date()
        yesterday_local = today_local - timedelta(days=1)

        last_day = None
        if latest:
            db_date_raw = latest[0]

            # Normaliza a data do SQLite para um date()
            if isinstance(db_date_raw, datetime):
                db_date = db_date_raw.date()
            elif isinstance(db_date_raw, date):
                db_date = db_date_raw
            else:
                # normalmente vem 'YYYY-MM-DD'
                db_date = datetime.strptime(str(db_date_raw), "%Y-%m-%d").date()

            if db_date == today_local:
                label = "Hoje"
            elif db_date == yesterday_local:
                label = "Ontem"
            else:
                label = "Último dia no DB"

            last_day = {
                "date": str(db_date),                 # mantém compatibilidade (YYYY-MM-DD)
                "date_br": db_date.strftime("%d/%m/%Y"),
                "label": label,
                "forwards": latest[1],
                "rebalances": latest[2],
                "profit": latest[3],
            }

        monthly = [
            {
                "month": row[0],
                "forwards": row[1],
                "rebalances": row[2],
                "profit": row[3],
            }
            for row in (months or [])
        ]

        year_to_date = ytd[2] if ytd else 0

        return jsonify({
            "last_day": last_day,
            "monthly": monthly,
            "year_to_date": year_to_date,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/top-peers")
def api_top_peers():
    """
    Top/bottom peers por fees de encaminhamento na janela especificada.
    Parâmetro opcional: ?days=7|30|90 (default 30).
    """
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30

    # Mantém em um intervalo razoável
    if days <= 0 or days > 365:
        days = 30

    data = get_top_forwarding_peers(days=days, limit=5)
    if isinstance(data, dict) and data.get("error"):
        return jsonify(data), 500
    return jsonify(data)

# -----------------------------
# Entrypoint (HTTPS self-signed)
# -----------------------------
if __name__ == '__main__':
    # Necessário para usar câmera no browser (QR) com HTTPS
    # Os arquivos cert-ns.pem e key-ns.pem devem existir na pasta do projeto
    app.run(host='0.0.0.0', port=5000, ssl_context=('cert-ns.pem', 'key-ns.pem'))
    # Se precisar usar HTTP simples (não recomendado), comente a linha acima e descomente:
    # app.run(host='0.0.0.0', port=5000)
