import datetime
from typing import Annotated, Optional

from fastapi import FastAPI, Depends, HTTPException, Response, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import update, delete
from sqlmodel import SQLModel, Session, select
from starlette import status
from starlette.responses import RedirectResponse

from personalpalate.orm.connect import prepare_db
from personalpalate.deps import (
    db_session,
    get_current_user,
    ensure_user_not_logged_in,
)
from personalpalate.orm.model import (
    Account,
    AccountDTO,
    Follow,
    Meal,
    MealDTO,
    Category,
    MealPlanDay,
    MealPlanDayDTO,
)
from personalpalate.security import password as passlib
from personalpalate.security.token import Token, create_access_token
from personalpalate.meal_rec import construct_pmf

app = FastAPI()

templates = Jinja2Templates("personalpalate/templates")
app.mount("/static", StaticFiles(directory="personalpalate/static"), name="static")


@app.on_event("startup")
def on_startup():
    prepare_db()


@app.get("/")
async def root(
    request: Request, account: Annotated[AccountDTO, Depends(get_current_user)]
):
    """Home landing page for signed in users"""

    return templates.TemplateResponse("home.html.jinja", {"request": request})


class FollowData(BaseModel):
    email: str


@app.post("/account/follow", status_code=201, response_model=Follow)
async def follow(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    email: FollowData,
):
    """Follows another user, if allowed"""

    other_acc = sess.exec(
        select(Account).where(Account.email == email.email).where(Account.followable)
    ).first()

    if not other_acc:
        raise HTTPException(
            400, "Other account is either not followable or does not exist"
        )

    if sess.exec(
        select(Follow)
        .where(Follow.followingEmail == account.email)
        .where(Follow.email == email.email)
    ).first():
        raise HTTPException(200, "Account is already following")

    f = Follow(followingEmail=account.email, email=email.email)
    sess.add(f)
    sess.commit()

    return f


@app.delete("/account/follow")
async def unfollow(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    email: FollowData,
):
    """
    Unfollows another user. Plans made using that users meals will still exist, but will however not be considered in
    the recommendations.
    """

    f = sess.exec(
        select(Follow)
        .where(Follow.followingEmail == account.email)
        .where(Follow.email == email.email)
    ).first()

    if not f:
        raise HTTPException(400, "Follow relationship does not exist")

    sess.delete(f)


class AccountSettings(BaseModel):
    followable: bool


@app.put("/account/settings", response_model=AccountDTO)
async def update_settings(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    settings: AccountSettings,
):
    """Updates user settings"""

    follow2 = settings.followable
    account = sess.get(Account, account.email)
    account.followable = follow2

    sess.add(account)
    sess.commit()

    return AccountDTO.from_orm(account)


@app.post("/meal", status_code=201, response_model=list[Meal])
async def add_meals(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    meals: list[MealDTO],
):
    """Adds many meals to the users account"""

    # Transform into DB model
    meal_rec = [Meal(email=account.email, **m.dict()) for m in meals]
    sess.add_all(meal_rec)

    sess.commit()

    return meal_rec


@app.delete("/meal")
async def delete_meals(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    meal: Meal,
):
    """
    Deletes a meal from the users account. If this removes all copies of a meal, then mealplans using it will still
    exist. However, they will not be considered for recommendation.
    """

    if meal.email != account.email:
        raise HTTPException(401, "User does not own meal")

    sess.delete(sess.get(Meal, meal.mealID))
    sess.commit()


@app.put("/meal", response_model=Meal)
async def update_meal(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    meal: Meal,
):
    """
    Updates a users meal. If the meal name is changed and this was the last copy of a meal, all mealplans with the same
    meal name will also be renamed to match.
    """

    if meal.email != account.email:
        raise HTTPException(401, "User does not own meal")

    db_meal = sess.get(Meal, meal.mealID)
    old_meal_name = db_meal.mealName

    for key, value in meal.dict().items():
        setattr(db_meal, key, value)
    sess.add(db_meal)
    sess.commit()

    emails = [
        a.followingEmail
        for a in sess.exec(select(Follow).where(Follow.email == account.email)).all()
    ] + [account.email]

    # If the meal name has changed such that no meals with that name exist, migrate meal plans to the new name
    for email in emails:
        if (
            len(
                sess.exec(
                    create_followed_meals_expr(sess, email, None).where(
                        Meal.mealName == old_meal_name
                    )
                ).all()
            )
            == 0
        ):
            sess.execute(
                update(MealPlanDay)
                .where(MealPlanDay.email == email)
                .where(MealPlanDay.mealName == old_meal_name)
                .values(mealName=meal.mealName)
            )

    sess.commit()
    return meal


@app.get("/meal", response_model=list[Meal])
async def get_meals(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    category: Optional[Category] = None,
):
    """Gets all meals from the users account. Optionally filters by category"""

    q = select(Meal).where(Meal.email == account.email)

    if category:
        q = q.where(Meal.category == category)

    meals = sess.exec(q).all()

    return meals


def create_followed_meals_expr(sess: Session, email: str, category: Optional[Category]):
    """Creates a Sqlmodel expression that gets all meals for this user and their followed users, for a given category"""
    # Create a list of all emails meals can be from
    followers = sess.exec(select(Follow).where(Follow.followingEmail == email)).all()
    follower_ids = [f.email for f in followers]
    follower_ids.append(email)

    # All meals from this user or followers, of a given category
    meals_q = select(Meal).where(Meal.email.in_(follower_ids))

    if category:
        meals_q = meals_q.where(Meal.category == category)

    return meals_q


def get_followed_meals(
    sess: Session, account: AccountDTO, category: Optional[Category]
) -> list[Meal]:
    """Gets all meals for this user and their followed users, for a given category"""

    return sess.exec(create_followed_meals_expr(sess, account.email, category)).all()


@app.get("/meal/followed", response_model=list[Meal])
async def get_meals_with_follower(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    category: Optional[Category] = None,
):
    """Gets all meals from the users account, and followed accounts. Optionally filters by category"""

    return get_followed_meals(sess, account, category)


def create_recc(
    sess: Session,
    account: AccountDTO,
    date: datetime.date,
    category: Optional[Category] = None,
) -> Optional[str]:
    """Creates a recommendation for a single day for a user. Returns meal name, or none if user has no meals in
    category. Pass no category to get a random meal category."""

    meals = get_followed_meals(sess, account, category)

    if len(meals) == 0:
        return None

    # List of (meal name, day chosen)
    past_choices: list[tuple[str, datetime.date]] = sess.exec(
        select(MealPlanDay.mealName, MealPlanDay.mealPlanDate).where(
            account.email == MealPlanDay.email
        )
    ).all()

    result = construct_pmf(meals, past_choices, date)

    return result


@app.get("/recommend", response_model=MealPlanDayDTO)
async def generate_recommend(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    day: datetime.date,
    category: Optional[Category] = None,
):
    """Creates a recommendation for a given day. Optionally filter by category."""
    result = create_recc(sess, account, day, category)

    if result is None:
        raise HTTPException(
            400, f"User does not have any meals of category: {category}"
        )

    return MealPlanDayDTO(mealName=result, mealPlanDate=day)


@app.put("/plans", status_code=200, response_model=MealPlanDayDTO)
async def update_plan(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
    chosen: MealPlanDayDTO,
):
    """Persists a chosen mealplan day, or overwrites an existing one"""

    # Update plan if exists
    old_plan = sess.exec(
        select(MealPlanDay)
        .where(MealPlanDay.email == account.email)
        .where(MealPlanDay.mealPlanDate)
    ).one_or_none()

    if old_plan:
        for key, value in chosen.dict(exclude_unset=True).items():
            setattr(old_plan, key, value)

    sess.add(old_plan or MealPlanDay(email=account.email, **chosen.dict()))
    sess.commit()

    return chosen


@app.get("/plans", response_model=list[MealPlanDayDTO])
async def get_plans(
    sess: Annotated[Session, Depends(db_session)],
    account: Annotated[AccountDTO, Depends(get_current_user)],
):
    """Gets all past meal plans the user has saved"""
    return sess.exec(
        select(MealPlanDay).where(MealPlanDay.email == account.email)
    ).all()


# Login stuff


@app.get("/login")
async def login(request: Request, not_login=Depends(ensure_user_not_logged_in)):
    """Login page. Will redirect to home if already logged in."""
    return templates.TemplateResponse("login.html.jinja", {"request": request})


@app.post("/signup", status_code=201, response_model=AccountDTO)
async def signup(
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    name: Annotated[str, Form()],
    sess: Annotated[Session, Depends(db_session)],
    not_login=Depends(ensure_user_not_logged_in),
):
    """Creates a new account"""
    if sess.get(Account, email):
        raise HTTPException(400, "user with email already exists")
    else:
        account = Account(
            email=email, password=passlib.password_context.hash(password), name=name
        )
        sess.add(account)
        sess.commit()

        return AccountDTO.from_orm(account)


@app.post("/token", response_model=Token)
async def create_token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], response: Response
):
    """Creates an OAuth2 token"""
    if passlib.verify_password(form.password, form.username):
        access_token = create_access_token(form.username)
        # Set token in cookie and respond as JSON
        response.set_cookie("access_token", f"bearer {access_token}", httponly=True)
        return {"access_token": access_token, "token_type": "bearer"}
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/logout")
async def logout():
    """Destroys the login token"""
    response = RedirectResponse(url="/login")
    response.delete_cookie("access_token")
    return response
