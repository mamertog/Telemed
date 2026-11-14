"""Segurança: hash de senhas (Argon2id), criptografia do segredo TOTP, permissões e auditoria."""
from __future__ import annotations

import base64
import hashlib
import secrets
from functools import wraps

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from flask import abort, current_app, request
from flask_login import current_user

from .extensions import db
from .models import AuditLog
from .utils import hashed_ip

_ph = PasswordHasher()  # Argon2id por padrão
_DUMMY_HASH = _ph.hash("senha-ficticia-para-tempo-constante")

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _ph.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def burn_password_check(password: str) -> None:
    """Executa uma verificação fictícia para igualar o tempo de resposta (anti-enumeração)."""
    verify_password(None, password)


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password or "") < MIN_PASSWORD_LENGTH:
        problems.append(f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
    if password and password.lower() in {"123456789012", "senhasenha12", "qwertyuiop12"}:
        problems.append("Escolha uma senha menos previsível.")
    return problems


def new_session_token() -> str:
    return secrets.token_hex(32)


def _fernet() -> Fernet:
    key_material = current_app.config.get("MFA_ENCRYPTION_KEY") or current_app.config["SECRET_KEY"]
    key = base64.urlsafe_b64encode(hashlib.sha256(("mfa|" + key_material).encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def permission_required(*perms: str):
    """Exige que o usuário autenticado possua TODAS as permissões informadas."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            for p in perms:
                if not current_user.can(p):
                    abort(403)
            return view(*args, **kwargs)
        wrapped._required_permissions = perms  # usado nos testes
        return wrapped
    return decorator


def audit(action: str, entity_type: str | None = None, entity_id=None, commit: bool = False) -> None:
    """Registra uma ação administrativa. Nunca registra conteúdo clínico nem dados pessoais."""
    user_id = current_user.id if getattr(current_user, "is_authenticated", False) else None
    db.session.add(AuditLog(
        user_id=user_id, action=action, entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        ip_hash=hashed_ip() if request else None,
    ))
    if commit:
        db.session.commit()
