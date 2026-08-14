"""Direct image command parsing and human-friendly Leon mode names."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

DEFAULT_NSFW_MODE_ID = "k2_queen_marika"

_MODE_NAMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "k2_red_craft": ("红艺", ("red-craft",)),
    "k2_dark_beast": ("暗黑兽", ("dark-beast",)),
    "k2_gpt": ("Krea GPT", ("gpt",)),
    "k2_mature_manhwa": ("成熟韩漫", ("mature-manhwa", "韩漫")),
    "k2_moody_mix": ("Moody 混合", ("moody-mix",)),
    "k2_tifa": ("蒂法", ("tifa",)),
    "k2_tifa_plus": ("蒂法增强", ("tifa-plus", "tifaplus", "蒂法加强")),
    "k2_eliska": ("艾莉丝卡", ("eliska",)),
    "k2_boa_hancock": ("波雅·汉库克", ("hancock", "汉库克", "女帝")),
    "k2_queen_marika": ("玛莉卡", ("marika", "玛丽卡", "女王玛莉卡")),
    "k2_shea": ("希娅", ("shea",)),
    "k2_girls_flux_v60": ("Girls Flux V6", ("girls-flux", "girls-flux-v6")),
    "k2_altgirl_v60": ("另类女孩 V6", ("altgirl", "altgirl-v6")),
    "noob_ciloranko": ("Ciloranko 插画", ("ciloranko",)),
    "nsfw": ("写实基础", ("real-basic",)),
    "nsfw_moodypromix": ("Moody ProMix", ("moody-promix",)),
    "moody_anima": ("Anima 动漫", ("anima",)),
    "nsfw_realcore": ("RealCore 写实", ("realcore",)),
    "nsfw_juggernaut": ("Juggernaut 写实", ("juggernaut",)),
}


@dataclass(frozen=True)
class DirectImageCommand:
    source_text: str
    workflow_id: str
    mode_name: str


def _normalize_mode_name(value: str) -> str:
    return re.sub(r"[\s_.\-·]+", "", value.strip().casefold())


def _available_mode_ids(modes: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get("id") or "").strip()
            for item in modes
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        )
    )


def mode_display_name(mode_id: str) -> str:
    return _MODE_NAMES.get(mode_id, (mode_id, ()))[0]


def mode_catalog_items(modes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for mode_id in _available_mode_ids(modes):
        name, aliases = _MODE_NAMES.get(mode_id, (mode_id, ()))
        items.append({"id": mode_id, "name": name, "aliases": list(aliases)})
    return items


def format_mode_catalog(modes: list[dict[str, Any]]) -> str:
    lines = [
        "可用生图模式：",
        "",
        "用法：`/nsfw --model <中文名或模式ID> <生图描述>`",
        f"不写 `--model` 时默认：**{mode_display_name(DEFAULT_NSFW_MODE_ID)}**",
        "",
    ]
    lines.extend(f"- **{item['name']}**：`{item['id']}`" for item in mode_catalog_items(modes))
    return "\n".join(lines)


def resolve_mode_id(selection: str, modes: list[dict[str, Any]]) -> str:
    mode_ids = _available_mode_ids(modes)
    aliases: dict[str, str] = {}
    for mode_id in mode_ids:
        label, configured_aliases = _MODE_NAMES.get(mode_id, (mode_id, ()))
        candidates = {mode_id, mode_id.replace("_", "-"), label, *configured_aliases}
        if mode_id.startswith("k2_"):
            candidates.add(mode_id[3:])
        if mode_id.startswith("nsfw_"):
            candidates.add(mode_id[5:])
        for candidate in candidates:
            aliases[_normalize_mode_name(candidate)] = mode_id

    resolved = aliases.get(_normalize_mode_name(selection))
    if resolved:
        return resolved
    raise ValueError(f"未知生图模式：{selection}")


def parse_nsfw_command(
    message: str,
    modes: list[dict[str, Any]],
) -> DirectImageCommand | None:
    """Parse `/nsfw [--model MODE] PROMPT`; None requests the mode catalog."""
    raw = message.strip()[5:].strip()
    if not raw:
        return None
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc
    if not tokens or tokens[0].casefold() in {"--models", "--list-models", "--help"}:
        return None

    selected_mode = DEFAULT_NSFW_MODE_ID
    prompt_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        folded = token.casefold()
        if folded in {"--model", "-m"}:
            index += 1
            if index >= len(tokens):
                raise ValueError("`--model` 后面需要模式名称")
            selected_mode = tokens[index]
        elif folded.startswith("--model="):
            selected_mode = token.split("=", 1)[1]
            if not selected_mode:
                raise ValueError("`--model` 后面需要模式名称")
        elif token.startswith("--"):
            raise ValueError(f"未知参数：{token}")
        else:
            prompt_parts.append(token)
        index += 1

    source_text = " ".join(prompt_parts).strip()
    if not source_text:
        raise ValueError("缺少生图描述")
    workflow_id = resolve_mode_id(selected_mode, modes)
    return DirectImageCommand(
        source_text=source_text,
        workflow_id=workflow_id,
        mode_name=mode_display_name(workflow_id),
    )
