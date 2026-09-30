from beehive.web.images import image_srcset, sized_image_url


def test_shopify_cdn_photo_asks_for_a_width_and_keeps_its_version():
    url = "https://cdn.shopify.com/s/files/1/0056/3141/0240/files/BetaPant.jpg?v=1745347973"

    assert sized_image_url(url, 240) == (
        "https://cdn.shopify.com/s/files/1/0056/3141/0240/files/BetaPant.jpg?v=1745347973&width=240"
    )


def test_shopify_width_replaces_one_already_there():
    url = "https://cdn.shopify.com/s/files/1/x.png?width=3000&v=7"

    assert sized_image_url(url, 600) == "https://cdn.shopify.com/s/files/1/x.png?v=7&width=600"


def test_store_domain_shopify_path_is_resized_too():
    url = "https://www.example.co.nz/cdn/shop/files/tent.jpg?v=1"

    assert sized_image_url(url, 300) == "https://www.example.co.nz/cdn/shop/files/tent.jpg?v=1&width=300"


def test_end_square_crop_takes_the_width_on_both_sides():
    url = (
        "https://media.endclothing.com/media/f_auto,q_auto:eco,w_400,h_400/prodmedia/media/"
        "catalog/product/2/8/six_m1.jpg"
    )

    assert sized_image_url(url, 800) == (
        "https://media.endclothing.com/media/f_auto,q_auto:eco,w_800,h_800/prodmedia/media/"
        "catalog/product/2/8/six_m1.jpg"
    )


def test_yoox_photo_moves_to_the_nearest_fixed_size():
    url = "https://www.yoox.com/images/items/16/16012453UX_11_f.jpg"

    assert sized_image_url(url, 240) == url
    assert sized_image_url(url, 600).endswith("/16012453UX_13_f.jpg")
    assert sized_image_url(url, 1000).endswith("/16012453UX_14_f.jpg")


def test_cdn_without_a_size_parameter_is_left_alone():
    for url in (
        "https://images-cdn.auctionmobility.com/is3/auctionmobility-static/q09Z-1-M7CI0/1-DG263D/1.jpg",
        "https://img.mytheresa.com/512/512/66/jpeg/catalog/product/e6/P00329670.jpg",
        "https://firstsoftware-bkt1.s3.object.akl.cloudlocal.nz/images/products/large/a.png",
    ):
        assert sized_image_url(url, 600) == url


def test_missing_photo_stays_missing():
    assert sized_image_url(None, 240) is None
    assert sized_image_url("", 240) == ""
    assert image_srcset(None, (300, 600)) is None


def test_srcset_is_offered_only_where_any_width_works():
    shopify = "https://cdn.shopify.com/s/files/1/x.jpg?v=2"

    assert image_srcset(shopify, (300, 600)) == (
        "https://cdn.shopify.com/s/files/1/x.jpg?v=2&width=300 300w, "
        "https://cdn.shopify.com/s/files/1/x.jpg?v=2&width=600 600w"
    )
    assert image_srcset("https://www.yoox.com/images/items/1/a_11_f.jpg", (300, 600)) is None
