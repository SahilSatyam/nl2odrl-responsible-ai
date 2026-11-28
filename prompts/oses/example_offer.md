## Example: Offer (assigner stated, action refinement)

Input: "Museum M offers access to print catalog-photos at up to 300 dpi for educational use."

```json
{
 "analysis": "Offer from Museum M specifying a print action refinement (resolution) and purpose constraint.",
 "policy_type": "Offer",
 "assigner": "Museum M",
 "rules": [
  {
   "modality": "permission",
   "action": "print",
   "action_refinements": [
    {"kind": "atomic", "left_operand": "resolution", "operator": "lteq", "right_operand": "300", "value_type": "integer", "right_concepts": []}
   ],
   "target": "catalog-photos",
   "constraints": [
    {"kind": "atomic", "left_operand": "purpose", "operator": "isA", "right_operand": "education", "value_type": "string", "right_concepts": []}
   ],
   "source_spans": [{"quote": "Museum M offers access to print catalog-photos at up to 300 dpi for educational use."}]
  }
 ],
 "ambiguities": [],
 "assumptions": []
}
```
