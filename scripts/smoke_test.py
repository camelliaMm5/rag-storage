"""PostgreSQL deployment smoke test."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 60)
print("[测试] 会话管理模块")
print("=" * 60)

try:
    from utils import ConversationManager
    print("[OK] 导入会话管理模块成功")
except ImportError as e:
    print(f"[FAIL] 导入失败: {e}")
    sys.exit(1)

print("[INFO] 测试数据库连接...")
try:
    cm = ConversationManager(max_context_turns=5)
    print("[OK] 数据库连接成功，表已创建")
except Exception as e:
    print(f"[FAIL] 数据库连接失败: {e}")
    sys.exit(1)

print("[INFO] 测试创建会话...")
conv_id = cm.create("test_user")
print(f"[OK] 创建会话成功: {conv_id}")

print("[INFO] 测试添加消息...")
mid1 = cm.add_message(conv_id, "user", "我的订单到哪了？")
mid2 = cm.add_message(conv_id, "assistant", "您的订单已发货")
mid3 = cm.add_message(conv_id, "user", "谢谢")
print(f"[OK] 添加消息成功，消息ID: {mid1}, {mid2}, {mid3}")

print("[INFO] 测试获取上下文...")
ctx = cm.get_context(conv_id)
print(f"[OK] 获取上下文成功，共 {len(ctx)} 条消息")

print("[INFO] 测试获取完整历史...")
hist = cm.get_history(conv_id)
print(f"[OK] 获取完整历史成功，共 {len(hist)} 条消息")

print("[INFO] 测试列出会话...")
conv_list = cm.list_conversations("test_user")
print(f"[OK] 列出会话成功，共 {len(conv_list)} 个会话")

print("[INFO] 测试关闭会话...")
cm.close(conv_id)
print("[OK] 关闭会话成功")

print()
print("[SUCCESS] 所有冒烟测试通过！")
