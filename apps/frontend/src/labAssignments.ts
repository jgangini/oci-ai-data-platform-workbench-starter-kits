export function labAssignmentChanges(
  assignedIds: string[],
  selectedIds: string[],
) {
  const assigned = new Set(assignedIds);
  const selected = new Set(selectedIds);
  return {
    add: selectedIds.filter((labId) => !assigned.has(labId)),
    remove: assignedIds.filter((labId) => !selected.has(labId)),
  };
}
