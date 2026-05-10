"""API integration tests — projects CRUD and tenancy isolation."""

import pytest
from httpx import AsyncClient

from tests.test_api_auth import _register_and_login


@pytest.mark.asyncio
class TestProjectsCRUD:
    async def test_create_project(self, client: AsyncClient):
        token = await _register_and_login(client)
        resp = await client.post("/api/projects", json={
            "name": "Test Project", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Project"
        assert data["industry"] == "insurance"

    async def test_list_projects(self, client: AsyncClient):
        token = await _register_and_login(client)
        await client.post("/api/projects", json={
            "name": "P1", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token}"})
        await client.post("/api/projects", json={
            "name": "P2", "industry": "finance",
        }, headers={"Authorization": f"Bearer {token}"})

        resp = await client.get("/api/projects", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert "P1" in names
        assert "P2" in names

    async def test_get_project(self, client: AsyncClient):
        token = await _register_and_login(client)
        create_resp = await client.post("/api/projects", json={
            "name": "MyProj", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token}"})
        pid = create_resp.json()["id"]

        resp = await client.get(f"/api/projects/{pid}", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "MyProj"

    async def test_get_nonexistent_project(self, client: AsyncClient):
        token = await _register_and_login(client)
        resp = await client.get("/api/projects/99999", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 404

    async def test_create_project_without_auth(self, client: AsyncClient):
        resp = await client.post("/api/projects", json={
            "name": "NoAuth", "industry": "insurance",
        })
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestBrands:
    async def test_add_and_list_brands(self, client: AsyncClient):
        token = await _register_and_login(client)
        create_resp = await client.post("/api/projects", json={
            "name": "BrandTest", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token}"})
        pid = create_resp.json()["id"]

        await client.post(f"/api/projects/{pid}/brands", json={
            "name": "主品牌", "aliases": ["别名A"], "is_competitor": False,
        }, headers={"Authorization": f"Bearer {token}"})

        await client.post(f"/api/projects/{pid}/brands", json={
            "name": "竞品", "is_competitor": True,
        }, headers={"Authorization": f"Bearer {token}"})

        resp = await client.get(f"/api/projects/{pid}/brands", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        brands = resp.json()
        assert len(brands) == 2
        names = [b["name"] for b in brands]
        assert "主品牌" in names
        assert "竞品" in names


@pytest.mark.asyncio
class TestPrompts:
    async def test_add_and_list_prompts(self, client: AsyncClient):
        token = await _register_and_login(client)
        create_resp = await client.post("/api/projects", json={
            "name": "PromptTest", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token}"})
        pid = create_resp.json()["id"]

        await client.post(f"/api/projects/{pid}/prompts", json={
            "text": "推荐一款保险产品", "category": "recommend",
        }, headers={"Authorization": f"Bearer {token}"})

        resp = await client.get(f"/api/projects/{pid}/prompts", headers={
            "Authorization": f"Bearer {token}",
        })
        assert resp.status_code == 200
        prompts = resp.json()
        assert len(prompts) == 1
        assert prompts[0]["text"] == "推荐一款保险产品"

    async def test_delete_prompt(self, client: AsyncClient):
        token = await _register_and_login(client)
        create_resp = await client.post("/api/projects", json={
            "name": "DelTest", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token}"})
        pid = create_resp.json()["id"]

        add_resp = await client.post(f"/api/projects/{pid}/prompts", json={
            "text": "待删除", "category": "evaluate",
        }, headers={"Authorization": f"Bearer {token}"})
        prompt_id = add_resp.json()["id"]

        del_resp = await client.delete(
            f"/api/projects/{pid}/prompts/{prompt_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert del_resp.status_code == 204

        list_resp = await client.get(f"/api/projects/{pid}/prompts", headers={
            "Authorization": f"Bearer {token}",
        })
        assert len(list_resp.json()) == 0


@pytest.mark.asyncio
class TestTenancyIsolation:
    """Verify users can only access their own projects."""

    async def test_cannot_see_other_users_project(self, client: AsyncClient):
        token_a = await _register_and_login(client, "user_a", "pw")
        token_b = await _register_and_login(client, "user_b", "pw")

        create_resp = await client.post("/api/projects", json={
            "name": "A's Project", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token_a}"})
        pid_a = create_resp.json()["id"]

        # User B tries to access User A's project
        resp = await client.get(f"/api/projects/{pid_a}", headers={
            "Authorization": f"Bearer {token_b}",
        })
        assert resp.status_code == 404

    async def test_cannot_add_brand_to_other_users_project(self, client: AsyncClient):
        token_a = await _register_and_login(client, "user_c", "pw")
        token_b = await _register_and_login(client, "user_d", "pw")

        create_resp = await client.post("/api/projects", json={
            "name": "C's Project", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token_a}"})
        pid_a = create_resp.json()["id"]

        resp = await client.post(f"/api/projects/{pid_a}/brands", json={
            "name": "Hacked", "is_competitor": False,
        }, headers={"Authorization": f"Bearer {token_b}"})
        assert resp.status_code == 404

    async def test_list_only_own_projects(self, client: AsyncClient):
        token_a = await _register_and_login(client, "user_e", "pw")
        token_b = await _register_and_login(client, "user_f", "pw")

        await client.post("/api/projects", json={
            "name": "E's Project", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token_a}"})
        await client.post("/api/projects", json={
            "name": "F's Project", "industry": "insurance",
        }, headers={"Authorization": f"Bearer {token_b}"})

        # User E should only see their own project
        resp = await client.get("/api/projects", headers={
            "Authorization": f"Bearer {token_a}",
        })
        names = [p["name"] for p in resp.json()]
        assert "E's Project" in names
        assert "F's Project" not in names
