"""Draft-only WordPress staging connector for approved newswire copy."""

import os
from urllib.parse import urljoin

import requests


class WordPressDraftPublisher:
    def __init__(self, site_url=None, username=None, app_password=None):
        self.site_url = (site_url or os.environ.get("NEWSWIRE_WORDPRESS_URL", "")).rstrip("/")
        self.username = username or os.environ.get("NEWSWIRE_WORDPRESS_USER", "")
        self.app_password = app_password or os.environ.get("NEWSWIRE_WORDPRESS_APP_PASSWORD", "")

    @property
    def configured(self):
        return bool(self.site_url and self.username and self.app_password)

    def _url(self, path):
        return urljoin(self.site_url + "/", "wp-json/wp/v2/" + path.lstrip("/"))

    def _request(self, method, path, **kwargs):
        if not self.configured:
            raise RuntimeError("WordPress staging is not configured")
        response = requests.request(
            method, self._url(path), auth=(self.username, self.app_password),
            timeout=30, **kwargs
        )
        if response.status_code >= 400:
            raise RuntimeError(f"WordPress returned HTTP {response.status_code}; check the staging connection")
        return response.json()

    def test_connection(self):
        data = self._request("GET", "users/me", params={"context": "edit"})
        return {"id": data.get("id"), "name": data.get("name", "")}

    def save_draft(self, title, html, existing_post_id=None):
        payload = {"title": title, "content": html, "status": "draft"}
        path = f"posts/{int(existing_post_id)}" if existing_post_id else "posts"
        data = self._request("POST", path, json=payload)
        return {
            "post_id": data["id"],
            "status": data.get("status", "draft"),
            "edit_url": f"{self.site_url}/wp-admin/post.php?post={data['id']}&action=edit",
            "link": data.get("link", ""),
        }
