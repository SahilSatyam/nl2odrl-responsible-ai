## Example: Set (no assigner, standalone obligation and prohibition)

Input: "Analysts must delete temp-logs within 30 days and must not transfer temp-logs."

```json
{
 "analysis": "Policy Set with no assigner, one standalone obligation and one prohibition.",
 "policy_type": "Set",
 "rules": [
  {
   "modality": "obligation",
   "action": "delete",
   "target": "temp-logs",
   "assignee": "Analysts",
   "constraints": [
    {"kind": "atomic", "left_operand": "elapsedTime", "operator": "lteq", "right_operand": "P30D", "value_type": "duration", "right_concepts": []}
   ],
   "source_spans": [{"quote": "Analysts must delete temp-logs within 30 days"}]
  },
  {
   "modality": "prohibition",
   "action": "transfer",
   "target": "temp-logs",
   "assignee": "Analysts",
   "source_spans": [{"quote": "must not transfer temp-logs"}]
  }
 ],
 "ambiguities": [],
 "assumptions": []
}
```
