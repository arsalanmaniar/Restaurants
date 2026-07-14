"""Favorite restaurants — a simple bookmark list, but it must be idempotent and
scoped to the right customer."""

from sqlalchemy import select

from app.models import CustomerFavorite
from app.services import conversations as convo
from app.services import tools


class TestAddFavorite:
    def test_add_favorite_by_id(self, db, conversation, pizza):
        result = tools.add_favorite(db, conversation, restaurant_id=pizza.id)
        assert result["favorited"] is True
        assert result["restaurant"] == pizza.name

        rows = db.scalars(
            select(CustomerFavorite).where(
                CustomerFavorite.customer_id == conversation.customer_id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].restaurant_id == pizza.id

    def test_add_favorite_by_name(self, db, conversation, pizza):
        result = tools.add_favorite(db, conversation, restaurant_name="Pizza Junction")
        assert result["favorited"] is True

    def test_adding_twice_is_idempotent(self, db, conversation, pizza):
        tools.add_favorite(db, conversation, restaurant_id=pizza.id)
        tools.add_favorite(db, conversation, restaurant_id=pizza.id)

        rows = db.scalars(
            select(CustomerFavorite).where(
                CustomerFavorite.customer_id == conversation.customer_id
            )
        ).all()
        assert len(rows) == 1  # no duplicate row, and no error from the unique constraint

    def test_unknown_restaurant_is_a_clean_error(self, db, conversation):
        result = tools.add_favorite(db, conversation, restaurant_name="Sushi Palace")
        assert result["error"] == "unknown_restaurant"


class TestRemoveFavorite:
    def test_remove_an_existing_favorite(self, db, conversation, pizza):
        tools.add_favorite(db, conversation, restaurant_id=pizza.id)
        result = tools.remove_favorite(db, conversation, restaurant_id=pizza.id)
        assert result["removed"] is True

        rows = db.scalars(
            select(CustomerFavorite).where(
                CustomerFavorite.customer_id == conversation.customer_id
            )
        ).all()
        assert rows == []

    def test_removing_a_favorite_that_was_never_added_does_not_error(
        self, db, conversation, pizza
    ):
        result = tools.remove_favorite(db, conversation, restaurant_id=pizza.id)
        assert result["removed"] is True


class TestListFavorites:
    def test_empty_list_for_a_new_customer(self, db, conversation):
        result = tools.list_favorites(db, conversation)
        assert result["favorites"] == []

    def test_lists_saved_favorites(self, db, conversation, pizza, biryani):
        tools.add_favorite(db, conversation, restaurant_id=pizza.id)
        tools.add_favorite(db, conversation, restaurant_id=biryani.id)

        names = {f["name"] for f in tools.list_favorites(db, conversation)["favorites"]}
        assert names == {pizza.name, biryani.name}

    def test_favorites_are_scoped_to_the_customer(self, db, conversation, pizza):
        tools.add_favorite(db, conversation, restaurant_id=pizza.id)

        other_customer = convo.get_or_create_customer(db, "923009998888")
        other_conversation = convo.get_or_create_conversation(db, other_customer)
        db.flush()

        assert tools.list_favorites(db, other_conversation)["favorites"] == []
