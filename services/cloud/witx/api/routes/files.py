"""Files a person gives a task, and the links a phone downloads them by."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from witx_contracts import api

from witx.api import views
from witx.api.deps import Deps
from witx.domain import rules
from witx.domain.rules import Rejected
from witx.services import files as file_service
from witx.services.identity import Actor
from witx.store import files


def routes(d: Deps) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    @router.post("/api/v1/files", status_code=201)
    async def upload_file(
        request: Request,
        who: CurrentActor,
        name: Annotated[str, Query(min_length=1, max_length=1024)],
    ) -> api.FileView:
        """The body is the file. It is the person's until they send it with a message
        (``file_ids``); one never sent is purged after a day."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit():
            rules.check_file_size(int(declared))
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            rules.check_file_size(len(data))
        async with d.db.tenant(who.tenant_id) as conn:
            kept = await file_service.upload(
                conn,
                d.objects,
                tenant_id=who.tenant_id,
                user_id=who.user_id,
                name=name,
                content_type=rules.media_type(request.headers.get("content-type")),
                data=bytes(data),
            )
        return views.file_view(kept)

    @router.get("/api/v1/downloads/{token}")
    async def download(token: str, request: Request) -> Response:
        """A file by its link, without a sign-in: the link is the key. In parts
        with ``Range``, so a phone resumes a download that dropped. A link that is not
        ours, has expired or names a file that is gone is not found, alike."""
        link = rules.read_link(token)
        if link is None:
            raise HTTPException(404, "not found")
        async with d.db.tenant(link.tenant_id) as conn:
            key = await files.link_key(conn, link.tenant_id)
            valid = key is not None and rules.link_valid(link, key, datetime.now(UTC))
            file = await files.get(conn, link.file_id) if valid else None
        if file is None or file.task_id is None:
            raise HTTPException(404, "not found")
        etag = f'"{file.sha256}"'
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Content-Disposition": rules.content_disposition(file.name),
            "Cache-Control": "private, no-store",
        }
        asked = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if if_range is not None and if_range != etag:
            asked = None  # the file changed since the part the phone holds: all of it
        try:
            part = rules.byte_range(asked, file.size)
        except Rejected:
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{file.size}"}
            )
        if file.size == 0:
            return Response(b"", media_type=file.content_type, headers=headers)
        start, end = part or (0, file.size - 1)
        headers["Content-Length"] = str(end - start + 1)
        if part is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{file.size}"
        return StreamingResponse(
            file_service.chunks(d.objects, link.tenant_id, file, start, end),
            status_code=206 if part is not None else 200,
            media_type=file.content_type,
            headers=headers,
        )

    return router
