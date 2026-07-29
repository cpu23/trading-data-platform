from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/investment")
def investment_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request,
        "investment.html",
        {"request": request},
    )
