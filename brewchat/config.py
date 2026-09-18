"""Supplier configuration.

Everything shop-specific (name, domain, endpoint paths, category ids) comes
from a TOML file. The real values live in the gitignored
``config/supplier.local.toml``; ``config/supplier.example.toml`` is the
committed placeholder copy. The app refuses to start without the local file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "config" / "supplier.example.toml"
LOCAL_CONFIG = REPO_ROOT / "config" / "supplier.local.toml"
ENV_FILE = REPO_ROOT / ".env"

INGREDIENT_TYPES = ("fermentable", "hop", "yeast", "other")
DEFAULT_MODEL = "claude-haiku-4-5"  # cheapest current model


class ConfigError(RuntimeError):
    """Raised when the supplier config is missing or malformed."""


@dataclass(frozen=True)
class SupplierSettings:
    name: str
    base_url: str
    products_all_path: str
    product_by_id_path: str
    currency: str
    prices_include_vat: bool
    user_agent: str

    @property
    def products_all_url(self) -> str:
        return self.base_url.rstrip("/") + self.products_all_path

    @property
    def vat_note(self) -> str:
        if self.prices_include_vat:
            return "Prices shown include VAT."
        return "Prices shown exclude VAT."


@dataclass(frozen=True)
class Settings:
    supplier: SupplierSettings
    cache_path: Path
    logs_dir: Path
    categories: dict[int, str]
    exclude_categories: frozenset[int] = field(default_factory=frozenset)
    passphrase: str | None = None
    model: str = DEFAULT_MODEL  # Claude model id, from $BREWCHAT_MODEL

    def ingredient_type_for(self, category_id: int, secondary_ids: list[int]) -> str | None:
        """Map a product's categories to an ingredient type, or None if not an ingredient."""
        if category_id in self.categories:
            return self.categories[category_id]
        if category_id in self.exclude_categories:
            return None
        for sid in secondary_ids:
            if sid in self.categories:
                return self.categories[sid]
        return None


def load_env_file() -> Path | None:
    """Load ``.env`` (or ``$BREWCHAT_ENV_FILE``) into ``os.environ``; real env vars win.

    Called by the entry points (web app, scripts), never at import time, so
    tests are not affected by a developer's local secrets. Set
    ``BREWCHAT_ENV_FILE=""`` to skip loading. Returns the file loaded, if any.
    """
    from dotenv import load_dotenv

    override = os.environ.get("BREWCHAT_ENV_FILE")
    if override == "":
        return None
    path = Path(override) if override else ENV_FILE
    if not path.is_file():
        return None
    load_dotenv(path, override=False)
    return path


def _resolve(path: str | Path, base: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else base / p


def load_settings(path: str | Path | None = None, *, base_dir: Path | None = None) -> Settings:
    """Load settings from ``path``, ``$BREWCHAT_CONFIG``, or the local config file.

    Relative paths inside the file (cache_path) resolve against ``base_dir``
    (default: repo root). ``$BREWCHAT_LOGS_DIR`` overrides the log directory.
    """
    base = base_dir or REPO_ROOT
    config_path = Path(path or os.environ.get("BREWCHAT_CONFIG") or LOCAL_CONFIG)
    if not config_path.is_file():
        raise ConfigError(
            f"Supplier config not found at {config_path}. Copy "
            f"{EXAMPLE_CONFIG.relative_to(REPO_ROOT)} to "
            f"{LOCAL_CONFIG.relative_to(REPO_ROOT)} and fill in the real shop values."
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        s = raw["supplier"]
        supplier = SupplierSettings(
            name=s["name"],
            base_url=s["base_url"],
            products_all_path=s["products_all_path"],
            product_by_id_path=s["product_by_id_path"],
            currency=s["currency"],
            prices_include_vat=bool(s["prices_include_vat"]),
            user_agent=s["user_agent"],
        )
        categories = {int(k): v for k, v in raw.get("categories", {}).items()}
        exclude = frozenset(int(i) for i in raw.get("exclude_categories", {}).get("ids", []))
        cache_path = _resolve(raw["sync"]["cache_path"], base)
    except (KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Supplier config {config_path.name} is malformed: {exc!r}") from exc

    bad = {v for v in categories.values() if v not in INGREDIENT_TYPES}
    if bad:
        raise ConfigError(f"Unknown ingredient types in [categories]: {sorted(bad)}")

    logs_dir = _resolve(os.environ.get("BREWCHAT_LOGS_DIR", "logs"), base)
    return Settings(
        supplier=supplier,
        cache_path=cache_path,
        logs_dir=logs_dir,
        categories=categories,
        exclude_categories=exclude,
        passphrase=os.environ.get("BREWCHAT_PASSPHRASE") or None,
        model=os.environ.get("BREWCHAT_MODEL") or DEFAULT_MODEL,
    )
