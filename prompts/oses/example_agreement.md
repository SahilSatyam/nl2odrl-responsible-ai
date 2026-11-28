## Example: Agreement (with set constraints, precondition duty, and remedy)

Input: "Data Steward permits Research Lab to read clinical-ds within the European Union for research if they attribute Data Steward. Research Lab must not distribute clinical-ds; if violated they must compensate Data Steward within 14 days."

```json
{
 "analysis": "Agreement between Data Steward and Research Lab with spatial/purpose constraints, a precondition duty, and a remedied prohibition.",
 "policy_type": "Agreement",
 "assigner": "Data Steward",
 "assignee": "Research Lab",
 "rules": [
  {
   "modality": "permission",
   "action": "read",
   "target": "clinical-ds",
   "constraints": [
    {"kind": "atomic", "left_operand": "spatial", "operator": "isPartOf", "right_operand": "European Union", "value_type": "string", "right_concepts": []},
    {"kind": "atomic", "left_operand": "purpose", "operator": "isA", "right_operand": "research", "value_type": "string", "right_concepts": []}
   ],
   "duties": [
    {"action": "attribute", "role": "precondition", "target": "Data Steward", "source_spans": [{"quote": "if they attribute Data Steward"}]}
   ],
   "source_spans": [{"quote": "Data Steward permits Research Lab to read clinical-ds within the European Union for research"}]
  },
  {
   "modality": "prohibition",
   "action": "distribute",
   "target": "clinical-ds",
   "duties": [
    {
     "action": "compensate",
     "role": "remedy",
     "target": "Data Steward",
     "deadline": {"kind": "atomic", "left_operand": "delayPeriod", "operator": "lteq", "right_operand": "P14D", "value_type": "duration", "right_concepts": []},
     "source_spans": [{"quote": "if violated they must compensate Data Steward within 14 days"}]
    }
   ],
   "source_spans": [{"quote": "Research Lab must not distribute clinical-ds"}]
  }
 ],
 "ambiguities": [],
 "assumptions": []
}
```
