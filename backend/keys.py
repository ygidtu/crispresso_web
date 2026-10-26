"""RSA 非对称密钥管理：私钥仅保留在服务端，用于解密前端传来的 OSS 凭据。

前端用公钥（RSA-OAEP）加密 OSS 信息后再提交，传输与落盘均密文；
即使内网有人截包或读取前端源码，没有私也无法解出密钥，从而保护 accessKeySecret。
"""
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

SECRETS_DIR = Path(__file__).resolve().parent.parent / ".secrets"
PRIVATE_KEY_PATH = SECRETS_DIR / "oss_rsa_private.pem"
KEY_SIZE = 2048


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_or_create_private_key() -> rsa.RSAPrivateKey:
    if PRIVATE_KEY_PATH.is_file():
        return serialization.load_pem_private_key(
            PRIVATE_KEY_PATH.read_bytes(), password=None
        )
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    # 私钥文件仅属主可读写，避免凭据落盘被其他用户读取
    PRIVATE_KEY_PATH.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    os.chmod(PRIVATE_KEY_PATH, 0o600)
    return key


_PRIVATE_KEY = _load_or_create_private_key()


def get_public_jwk() -> dict:
    """返回供前端 Web Crypto 直接 import 的 RSA 公钥 JWK。"""
    public_numbers = _PRIVATE_KEY.public_key().public_numbers()
    n_bytes = public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")
    return {
        "kty": "RSA",
        "n": _b64url(n_bytes),
        "e": _b64url(e_bytes),
        "alg": "RSA-OAEP-256",  # 与前端 importKey 的 hash SHA-256 匹配
        "use": "enc",
    }


def decrypt_token(b64_ciphertext: str) -> bytes:
    """前端 RSA-OAEP 密文（base64url）解密为原始明文字节。"""
    pad = base64.urlsafe_b64decode(b64_ciphertext + "=" * (-len(b64_ciphertext) % 4))
    return _PRIVATE_KEY.decrypt(pad, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(),
        label=None,
    ))
