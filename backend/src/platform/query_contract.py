"""Strict, domain-neutral query shape validation for public resource boundaries."""

from fastapi import Depends, HTTPException, Request


def strict_query(*allowed: str):
    def validate(request: Request):
        query = request.query_params
        if set(query) - set(allowed) or any(
            len(query.getlist(key)) != 1 or not query[key].strip() for key in query
        ):
            raise HTTPException(422, "Use non-empty, unambiguous canonical query selectors.")

    return Depends(validate)
