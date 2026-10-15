// The little prop that pops in next to the pet during a care action.
// Mounted by PetView while pendingAction is set, unmounted when the
// action animation ends — so its lifetime tracks the action with no
// timers of its own. The entrance animation is keyed on the action so
// re-triggering the same action replays the pop-in.

import { ActionKind } from "../api/petApi";
import { actionObject } from "../pet/objects";

export default function ActionObject({
  action,
}: {
  action: ActionKind;
}): JSX.Element {
  const obj = actionObject(action);
  return (
    <div
      key={action}
      className={`action-object action-object--${obj.motion} action-object--${obj.anchor}`}
      role="img"
      aria-label={obj.label}
    >
      {obj.emoji}
    </div>
  );
}
