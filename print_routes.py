from app.main import app

for route in app.routes:
    try:
        print(route.path, route.methods)
    except:
        print(route.path, "no methods")
