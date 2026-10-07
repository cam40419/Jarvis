"""Workspace directory rows have stable pagination and tenant-local identities."""

from uuid import UUID, uuid4

from simon.domain.identity import Membership


def test_workspace_directory_filters_scope_and_pages_without_managed_accounts(store):
    workspace, other = uuid4(), uuid4()
    members = [
        Membership(
            actor_id=UUID(int=value),
            workspace_id=workspace,
            role=role,
            display_name=f"Person {value}",
            workspace_name="Shared workspace",
        )
        for value, role in ((3, "guest"), (1, "owner"), (2, "member"))
    ]
    for member in members:
        store.put_membership(member)
    store.put_membership(
        Membership(actor_id=uuid4(), workspace_id=other, role="owner", display_name="Foreign")
    )
    ordered = sorted(members, key=lambda member: member.actor_id)
    assert store.workspace_members(workspace, 0, 2) == tuple(ordered[:2])
    assert store.workspace_members(workspace, 2, 2) == (ordered[2],)
    assert store.workspace_members(workspace, 3, 2) == ()
    assert store.workspace_members(uuid4(), 0, 2) == ()
    assert all(store.managed_account(member.actor_id) is None for member in ordered)
