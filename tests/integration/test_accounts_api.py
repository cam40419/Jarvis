from uuid import uuid4

from tests.passkey_helper import SoftwarePasskey


def test_invited_account_can_start_with_username_and_password(client, auth_headers):
    invited = client.post(
        "/v1/accounts/invite",
        headers=auth_headers,
        json={"display_name": "Password User", "idempotency_key": str(uuid4())},
    )
    assert invited.status_code == 200, invited.text
    token = invited.json()["enrollment_token"]
    client.cookies.clear()
    registered = client.post(
        "/auth/password/register",
        headers={"Origin": "http://localhost:8000"},
        json={
            "token": token,
            "username": "password.user",
            "password": "a private account phrase with many words",
        },
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["method"] == "password"
    assert registered.json()["actor_id"] == invited.json()["account"]["actor_id"]
    assert client.get("/v1/threads").status_code == 200


def test_password_recovery_code_resets_password_and_revokes_sessions(client, auth_headers):
    admin_cookie = client.cookies.get("simon_session")
    invited = client.post(
        "/v1/accounts/invite",
        headers=auth_headers,
        json={"display_name": "Recovering User", "idempotency_key": str(uuid4())},
    ).json()
    identifier = invited["account"]["actor_id"]
    client.cookies.clear()
    registered = client.post(
        "/auth/password/register",
        headers={"Origin": "http://localhost:8000"},
        json={
            "token": invited["enrollment_token"],
            "username": "recover.user",
            "password": "an initial private password phrase",
        },
    )
    assert registered.status_code == 200
    old_cookie = client.cookies.get("simon_session")
    client.cookies.clear()
    client.cookies.set("simon_session", admin_cookie)
    assert client.post(f"/v1/accounts/{identifier}/password-recovery", json={}).status_code == 403
    issued = client.post(
        f"/v1/accounts/{identifier}/password-recovery", headers=auth_headers, json={}
    )
    assert issued.status_code == 200
    code = issued.json()["enrollment_token"]
    client.cookies.clear()
    bad = client.post(
        "/auth/password/reset",
        headers={"Origin": "http://localhost:8000"},
        json={
            "token": code,
            "username": "other.user",
            "password": "a different secure password phrase",
        },
    )
    assert bad.status_code == 401
    reset = client.post(
        "/auth/password/reset",
        headers={"Origin": "http://localhost:8000"},
        json={
            "token": code,
            "username": "recover.user",
            "password": "a different secure password phrase",
        },
    )
    assert reset.status_code == 200
    assert reset.json()["method"] == "password"
    assert client.get("/v1/threads").status_code == 200
    client.cookies.clear()
    client.cookies.set("simon_session", old_cookie)
    assert client.get("/auth/session").status_code == 401
    client.cookies.clear()
    assert (
        client.post(
            "/auth/password/reset",
            headers={"Origin": "http://localhost:8000"},
            json={
                "token": code,
                "username": "recover.user",
                "password": "another different password phrase",
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/password/login",
            headers={"Origin": "http://localhost:8000"},
            json={"username": "recover.user", "password": "an initial private password phrase"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/password/login",
            headers={"Origin": "http://localhost:8000"},
            json={"username": "recover.user", "password": "a different secure password phrase"},
        ).status_code
        == 200
    )


def test_account_api_invite_isolation_and_revocation(client, container, auth_headers):
    admin_token = client.cookies.get("simon_session")
    admin_household = client.get("/auth/session").json()["household_id"]
    assert client.get("/auth/session").json()["can_manage_accounts"]
    body = {"display_name": "Alex", "idempotency_key": str(uuid4())}
    assert client.post("/v1/accounts/invite", json=body).status_code == 403
    assert (
        client.post(
            "/v1/accounts/invite",
            headers=auth_headers,
            json=body | {"household_id": admin_household},
        ).status_code
        == 422
    )
    result = client.post("/v1/accounts/invite", headers=auth_headers, json=body)
    assert result.status_code == 200, result.text
    invitation = result.json()
    identifier = invitation["account"]["actor_id"]
    assert invitation["account"]["household_id"] != admin_household
    assert "enrollment_hash" not in result.text
    assert (
        client.post("/v1/accounts/invite", headers=auth_headers, json=body).json()[
            "enrollment_token"
        ]
        is None
    )
    renewed = client.post(
        "/v1/accounts/" + identifier + "/invitation", headers=auth_headers, json={}
    )
    assert renewed.status_code == 200
    client.cookies.clear()  # Enrollment occurs in a separate browser session.
    key = SoftwarePasskey()
    start = client.post(
        "/auth/passkeys/register/options",
        headers=auth_headers,
        json={"token": renewed.json()["enrollment_token"]},
    ).json()
    member = client.post(
        "/auth/passkeys/register/verify",
        headers=auth_headers,
        json={
            "ceremony_id": start["ceremony_id"],
            "credential": key.response(start["options"], registration=True),
        },
    )
    assert member.status_code == 200 and not member.json()["can_manage_accounts"]
    member_token = client.cookies.get("simon_session")
    member_headers = {
        "Origin": "http://localhost:8000",
        "X-CSRF-Token": member.json()["csrf_token"],
    }
    assert client.get("/v1/accounts").status_code == 403
    assert client.post("/v1/accounts/invite", headers=member_headers, json=body).status_code == 403
    assert client.get("/v1/connections/home").json()["configured"] is False
    assert client.get("/v1/threads").json() == []
    assert client.get("/v1/memories").json() == []
    assert (
        client.post(
            "/auth/household", headers=member_headers, json={"household_id": admin_household}
        ).status_code
        == 403
    )
    client.cookies.clear()
    client.cookies.set("simon_session", admin_token)
    headers = auth_headers
    assert container.identity.resolve(member_token)[0].actor_id.hex
    account = client.get("/v1/accounts").json()[0]
    assert account["status"] == "active"
    assert (
        client.post(
            "/v1/accounts/" + identifier + "/invitation", headers=headers, json={}
        ).status_code
        == 409
    )
    access = client.post(
        "/v1/accounts/" + identifier + "/access",
        headers=headers,
        json={"enabled": False, "expected_version": account["version"]},
    )
    assert access.status_code == 200 and access.json()["status"] == "disabled"
    client.cookies.clear()
    client.cookies.set("simon_session", member_token)
    assert client.get("/v1/threads").status_code == 401
