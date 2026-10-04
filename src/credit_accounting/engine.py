"""确定性核算引擎（纯函数）。

设计原则：
- 无 I/O、无时钟、无随机：相同输入必定得到相同输出；
- 全程 ``Decimal`` + ``ROUND_HALF_UP``，避免浮点导致跨机复算漂移；
- 每条车型贡献保留逐步计算解释，可直接向企业/监管展示；
- 引擎逻辑由 ``ENGINE_VERSION`` 标记；规则阈值放在规则版本里，
  因此“规则勘误”只需发布新规则版本，无需改动代码。
"""
from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

from . import ENGINE_VERSION
from .errors import ValidationError
from .models import ENERGY_BEV, ENERGY_FCV, ENERGY_ICE, ENERGY_PHEV, NEV_TYPES

CENT = Decimal("0.01")


def D(value) -> Decimal:
    """把 JSON 中的数值（int/float/str）统一转为 Decimal。

    允许 float：Python 3 的 ``str(float)`` 为最短往返表示，版本内容入库时按
    规范 JSON 字节序列化并计算内容哈希，同一输入跨进程仍可稳定复算；
    但新接入方推荐直接传字符串以彻底规避二进制浮点歧义。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValidationError("布尔值不能作为核算数值")
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    raise ValidationError(f"不支持的数值类型：{type(value).__name__}")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, value))


def _require_number(content: dict, key: str, where: str) -> Decimal:
    if key not in content:
        raise ValidationError(f"{where}缺少数值字段：{key}")
    try:
        return D(content[key])
    except ValidationError:
        raise
    except Exception:
        raise ValidationError(f"{where}字段 {key} 不是合法数值") from None


# ---------------------------------------------------------------- 单车型核算

def compute_model_line(
    *,
    model_code: str,
    energy: dict,
    production: dict,
    rule: dict,
    policy: dict,
    production_version_id: str,
    energy_version_id: str,
    rule_version_id: str,
    policy_version_id: str,
) -> dict:
    """计算单个车型在给定四组版本下的贡献，返回带解释的分录行。

    返回结构（全部数值序列化为字符串，保证 JSON 与哈希稳定）：
    ``model_code / energy_type / quantity / unit_credit / subtotal /
       multiplier / final_credit / steps / *_version_id``
    """
    energy_type = energy.get("energy_type")
    if energy_type not in (ENERGY_BEV, ENERGY_PHEV, ENERGY_FCV, ENERGY_ICE):
        raise ValidationError(f"车型 {model_code} 的 energy_type 非法：{energy_type!r}")

    quantity = _require_number(production, "quantity", f"车型 {model_code} 产量版本")
    if quantity < 0:
        raise ValidationError(f"车型 {model_code} 产量不能为负")

    steps: list[dict] = []
    if energy_type in NEV_TYPES:
        multiplier = _require_number(policy, "nev_multiplier", "政策系数版本")
        mult_name = "nev_multiplier"
    else:
        multiplier = _require_number(policy, "cafc_multiplier", "政策系数版本")
        mult_name = "cafc_multiplier"

    if energy_type == ENERGY_BEV:
        cfg = rule["bev"]
        r = _require_number(energy, "range_km", f"车型 {model_code} 能耗版本")
        e = _require_number(energy, "kwh_per_100km", f"车型 {model_code} 能耗版本")
        r0 = _require_number(cfg, "range_threshold_km", "规则(bev)")
        e_ref = _require_number(cfg, "energy_ref_kwh", "规则(bev)")
        sens = _require_number(cfg, "energy_sensitivity", "规则(bev)")
        f_min = _require_number(cfg, "factor_min", "规则(bev)")
        f_max = _require_number(cfg, "factor_max", "规则(bev)")

        range_part = (r - r0) / D(100)
        steps.append({
            "step": "续航里程分",
            "formula": "(range_km - range_threshold_km) / 100",
            "inputs": {"range_km": str(r), "range_threshold_km": str(r0)},
            "output": str(range_part),
        })
        raw_factor = D(1) + (e_ref - e) / e_ref * sens
        factor = clamp(raw_factor, f_min, f_max)
        steps.append({
            "step": "能耗调整系数",
            "formula": "clip(1 + (energy_ref_kwh - kwh_per_100km) / energy_ref_kwh * energy_sensitivity, factor_min, factor_max)",
            "inputs": {
                "energy_ref_kwh": str(e_ref),
                "kwh_per_100km": str(e),
                "energy_sensitivity": str(sens),
                "factor_min": str(f_min),
                "factor_max": str(f_max),
            },
            "output": str(factor),
        })
        unit_credit = q2(range_part * factor)

    elif energy_type == ENERGY_PHEV:
        cfg = rule["phev"]
        r = _require_number(energy, "range_km", f"车型 {model_code} 能耗版本")
        r0 = _require_number(cfg, "range_threshold_km", "规则(phev)")
        base = _require_number(cfg, "base_credit", "规则(phev)")
        unit_credit = q2(base + (r - r0) / D(100))
        steps.append({
            "step": "插电混动单车积分",
            "formula": "base_credit + (range_km - range_threshold_km) / 100",
            "inputs": {"base_credit": str(base), "range_km": str(r), "range_threshold_km": str(r0)},
            "output": str(unit_credit),
        })

    elif energy_type == ENERGY_FCV:
        cfg = rule["fcv"]
        unit_credit = q2(_require_number(cfg, "unit_credit", "规则(fcv)"))
        steps.append({
            "step": "燃料电池单车积分",
            "formula": "unit_credit（规则固定值）",
            "inputs": {"unit_credit": str(unit_credit)},
            "output": str(unit_credit),
        })

    else:  # ENERGY_ICE —— 油耗积分，超标为负
        cfg = rule["ice"]
        actual = _require_number(energy, "fuel_l_per_100km", f"车型 {model_code} 能耗版本")
        target = _require_number(energy, "target_l_per_100km", f"车型 {model_code} 能耗版本")
        weight = _require_number(cfg, "cafc_weight", "规则(ice)")
        unit_credit = q2((target - actual) / target * weight)
        steps.append({
            "step": "CAFC 单车积分（可为负）",
            "formula": "(target_l_per_100km - fuel_l_per_100km) / target_l_per_100km * cafc_weight",
            "inputs": {
                "fuel_l_per_100km": str(actual),
                "target_l_per_100km": str(target),
                "cafc_weight": str(weight),
            },
            "output": str(unit_credit),
        })

    subtotal = q2(unit_credit * quantity)
    final_credit = q2(subtotal * multiplier)
    steps.append({
        "step": "产量小计",
        "formula": "unit_credit * quantity",
        "inputs": {"unit_credit": str(unit_credit), "quantity": str(quantity)},
        "output": str(subtotal),
    })
    steps.append({
        "step": "年度政策系数",
        "formula": mult_name,
        "inputs": {mult_name: str(multiplier)},
        "output": str(final_credit),
        "note": "subtotal * 系数",
    })

    return {
        "model_code": model_code,
        "energy_type": energy_type,
        "quantity": str(quantity),
        "unit_credit": str(unit_credit),
        "subtotal": str(subtotal),
        "multiplier": str(multiplier),
        "final_credit": str(final_credit),
        "steps": steps,
        "production_version_id": production_version_id,
        "energy_version_id": energy_version_id,
        "rule_version_id": rule_version_id,
        "policy_version_id": policy_version_id,
    }


# ---------------------------------------------------------------- 整期试算

def _validate_rule(rule_content: dict) -> dict:
    if str(rule_content.get("engine")) != ENGINE_VERSION:
        raise ValidationError(
            f"规则版本引擎 {rule_content.get('engine')!r} 与当前引擎 {ENGINE_VERSION} 不匹配"
        )
    for section in ("bev", "phev", "fcv", "ice"):
        if section not in rule_content or not isinstance(rule_content[section], dict):
            raise ValidationError(f"规则内容缺少配置段：{section}")
    return rule_content


def canonical_input_hash(snapshot: dict) -> str:
    """对快照输入计算稳定哈希（不依赖 dict 插入顺序、不依赖浮点格式）。"""
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def calculate(snapshot: dict, *, versions: dict) -> dict:
    """根据快照（版本指针集合）与版本内容执行整期试算。

    ``snapshot`` 结构::

        {
          "enterprise_id": "...", "year": 2025,
          "rule_version_id": "rv_...", "policy_version_id": "pv_...",
          "models": [{"model_code": "...", "production_version_id": ...,
                      "energy_version_id": ...}],
          "engine_version": ENGINE_VERSION
        }

    ``versions`` 为 ``{version_id: content_dict}``。
    返回 ``lines / total_credit / input_hash / engine_version``。
    """
    rule = _validate_rule(versions[snapshot["rule_version_id"]])
    policy = versions[snapshot["policy_version_id"]]

    lines: list[dict] = []
    total = Decimal("0")
    for ptr in sorted(snapshot["models"], key=lambda m: m["model_code"]):
        code = ptr["model_code"]
        pvid = ptr["production_version_id"]
        evid = ptr["energy_version_id"]
        production = versions[pvid]
        energy = versions[evid]
        line = compute_model_line(
            model_code=code,
            energy=energy,
            production=production,
            rule=rule,
            policy=policy,
            production_version_id=pvid,
            energy_version_id=evid,
            rule_version_id=snapshot["rule_version_id"],
            policy_version_id=snapshot["policy_version_id"],
        )
        lines.append(line)
        total += D(line["final_credit"])

    total = q2(total)
    input_hash = canonical_input_hash(snapshot)
    return {
        "engine_version": ENGINE_VERSION,
        "rule_version_id": snapshot["rule_version_id"],
        "policy_version_id": snapshot["policy_version_id"],
        "input_hash": input_hash,
        "lines": lines,
        "total_credit": str(total),
    }
