## OSES Duty Roles and Achievement Semantics

- **Policy-level obligation (`modality: "obligation"`)**: a standalone duty ("must" / "shall") that requires an `assignee`.
- **Permission precondition (`role: "precondition"`)**: a duty in a `permission` rule's `duties` list that must be satisfied to exercise the permission (e.g., "may use X if they attribute the steward" -> `action: "attribute", role: "precondition"`).
- **Prohibition remedy (`role: "remedy"`)**: a sanction or compensation in a `prohibition` rule's `duties` list triggered if the prohibition is violated (e.g., "must not distribute X; if violated must compensate" -> `action: "compensate", role: "remedy"`). Never attach a `remedy` to a permission.
- **Consequence (`role: "consequence"`)**: a fallback duty attached to a `modality: "obligation"` rule's `duties`, or nested inside a `precondition` duty's `consequences`.
- **Deadline**: an optional atomic `ConstraintIR` in `duty.deadline` when the duty has a time bound.
- **Achievement-only duties**: ODRL covers achievement actions only. When text states ongoing maintenance ("keep", "maintain", "as long as"), emit an `ambiguities` entry (`kind: "other"`) rather than inventing a duty.
