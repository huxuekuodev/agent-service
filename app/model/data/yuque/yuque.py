

import httpx


class YuqueClient:
    def __init__(self, token: str, user_id: str = "huxuekuo"):
        self.token = token
        self.user_id = user_id
        self.base_url = "https://www.yuque.com/api/v2"
        self.client = httpx.AsyncClient(headers={
            "X-Auth-Token": f"{token}",
            "User-Agent": "YuqueClient/1.0"
        })

    async def get_groups_list(self) -> dict:
        """获取用户所有知识库列表"""
        response = await self.client.get(
                f"{self.base_url}/groups/{self.user_id}/repos"
            )
        return response.json()

    async def get_books_list(self, group_id: str) -> dict:
        """获取知识库下的所有文档列表"""
        response = await self.client.get(
                f"{self.base_url}/repos/{group_id}/docs"
            )
        return response.json()

    async def get_document(self, document_id: str) -> dict:
        """获取文档详情"""
        response = await self.client.get(
                f"{self.base_url}/repos/docs/{document_id}"
            )
        return response.json()

async def main():
    client = YuqueClient(token="1OAorGvz6WCAGgDDv5RV7GFRCn30iJu3diZCbqVQ")
    groups = await client.get_groups_list()
    group_id = groups["data"][2]["id"]
    books = await client.get_books_list(group_id)
    print(books["data"][0])
    document_id = books["data"][0]["id"]
    document = await client.get_document(document_id)
    print(document["data"]["body_html"])



if __name__ == "__main__":
    import asyncio
    print("开始获取知识库列表...")
    asyncio.run(main())