# Shop plugin models package
from .database import get_shop_db, init_shop_db

# 插件内部统一通过本包导入连接函数（§9.1：走插件统一连接工厂）
get_db = get_shop_db
