import hashlib
from typing import Any, Dict




def evidence_hash(evidence: Dict[str, Any]) -> str:
   items = []
   for k in sorted(evidence.keys()):
       v = evidence.get(k)
       items.append(f"{k}={'' if v is None else str(v)}")
   blob = "|".join(items)
   return hashlib.sha256(blob.encode("utf-8")).hexdigest()




def disclosure_hash(disclosure: str) -> str:
   return hashlib.sha256((disclosure or "").strip().encode("utf-8")).hexdigest()




def build_evidence_pack(evidence: Dict[str, Any]) -> Dict[str, Any]:
   out = dict(evidence)
   if "disclosure" in out:
       out["disclosure_hash"] = disclosure_hash(str(out.get("disclosure") or ""))
   out["evidence_hash"] = evidence_hash(out)
   return out