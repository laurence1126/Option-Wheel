from pathlib import Path
from futu import *
from trading.utils.futu_utils import get_trading_pwd

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SysConfig.enable_proto_encrypt(True)
SysConfig.set_init_rsa_file(str(PROJECT_ROOT / ".RSA_private_key"))

FUTU_OPEND_ADDRESS = "100.64.0.1"
FUTU_OPEND_PORT = 22222

TRADING_ENVIRONMENT = TrdEnv.SIMULATE
TRADING_MARKET = TrdMarket.US
TRADING_PWD = get_trading_pwd(str(PROJECT_ROOT / ".config"))
