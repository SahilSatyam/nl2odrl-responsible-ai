## OSES Syntax and Grounding Guide

- **Policy types (`policy_type`)**:
  - `Agreement`: both `assigner` (granter) and `assignee` (recipient) are named.
  - `Offer`: `assigner` is named, open to any `assignee` (or rule-specific).
  - `Set`: general rules when no `assigner` is stated.
- **Rules (`rules`)**: one `modality` (`permission`, `prohibition`, `obligation`) and one `action` per rule.
- **Rule `constraints` vs `action_refinements`**:
  - Use `constraints` for conditions on when/where/why the rule applies (`dateTime`, `spatial`, `purpose`, `count`, `elapsedTime`, `delayPeriod`).
  - Use `action_refinements` only to qualify *how* the action itself is performed (e.g., `resolution lteq 300` on `print`, `fileFormat eq "csv"`, `payAmount`, `deliveryChannel`).
- **Set operators for place and purpose (no invented IRIs)**:
  - "in <place>" or "within <place>" -> `left_operand: "spatial"`, `operator: "isPartOf"`, `right_operand: "<place>"`, `value_type: "string"`, `right_concepts: []`.
  - "for <purpose>" -> `left_operand: "purpose"`, `operator: "isA"`, `right_operand: "<purpose>"`, `value_type: "string"`, `right_concepts: []`.
  - Always keep `right_concepts` as `[]`; downstream knowledge bases resolve concepts deterministically. Never invent IRIs or use `location` (use `spatial`), and never place an action name (such as `use` or `read`) in `left_operand`.
