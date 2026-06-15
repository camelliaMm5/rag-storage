-- ============================================
-- 电商平台 init.sql
-- shop schema (电商) + customer_service schema (AI客服)
-- ============================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================
-- shop schema
-- ============================================
CREATE SCHEMA IF NOT EXISTS shop;

-- 商品分类
CREATE TABLE shop.categories (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    parent_id   INTEGER REFERENCES shop.categories(id),
    sort_order  INTEGER DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 商品
CREATE TABLE shop.products (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    price       DECIMAL(10,2) NOT NULL,
    image_url   VARCHAR(500),
    stock       INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
    category_id INTEGER NOT NULL REFERENCES shop.categories(id),
    status      VARCHAR(20) DEFAULT 'on_sale',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_products_category ON shop.products(category_id);
CREATE INDEX idx_products_status  ON shop.products(status);

-- 用户
CREATE TABLE shop.users (
    id          SERIAL PRIMARY KEY,
    email       VARCHAR(255) NOT NULL UNIQUE,
    password    VARCHAR(255) NOT NULL,
    nickname    VARCHAR(100),
    role        VARCHAR(20) DEFAULT 'user',
    address     TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 购物车
CREATE TABLE shop.cart_items (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES shop.users(id),
    product_id  INTEGER NOT NULL REFERENCES shop.products(id),
    quantity    INTEGER NOT NULL DEFAULT 1 CHECK(quantity > 0),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

-- 订单
CREATE TABLE shop.orders (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES shop.users(id),
    total_amount    DECIMAL(10,2) NOT NULL,
    status          VARCHAR(20) DEFAULT 'pending',
    address         TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    paid_at         TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ
);
CREATE INDEX idx_orders_user   ON shop.orders(user_id);
CREATE INDEX idx_orders_status ON shop.orders(status);
CREATE INDEX idx_orders_pending_time ON shop.orders(created_at) WHERE status = 'pending';

-- 订单明细（快照）
CREATE TABLE shop.order_items (
    id            SERIAL PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES shop.orders(id),
    product_id    INTEGER NOT NULL,
    product_name  VARCHAR(255) NOT NULL,
    price         DECIMAL(10,2) NOT NULL,
    quantity      INTEGER NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 支付记录
CREATE TABLE shop.payment_records (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES shop.orders(id),
    amount      DECIMAL(10,2) NOT NULL,
    method      VARCHAR(50) DEFAULT 'mock',
    status      VARCHAR(20) DEFAULT 'success',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 物流记录
CREATE TABLE shop.logistics_records (
    id                  SERIAL PRIMARY KEY,
    order_id            INTEGER NOT NULL REFERENCES shop.orders(id),
    tracking_number     VARCHAR(100) NOT NULL,
    carrier             VARCHAR(50) DEFAULT 'SF-Express',
    status              VARCHAR(30) DEFAULT 'picked_up',
    current_location    VARCHAR(255),
    estimated_delivery  TIMESTAMPTZ,
    timeline            JSONB DEFAULT '[]',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_logistics_order ON shop.logistics_records(order_id);

-- 售后申请
CREATE TABLE shop.after_sale_requests (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES shop.users(id),
    order_id    INTEGER NOT NULL REFERENCES shop.orders(id),
    type        VARCHAR(20) NOT NULL,
    reason      TEXT,
    status      VARCHAR(20) DEFAULT 'pending',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_after_sale_user  ON shop.after_sale_requests(user_id);
CREATE INDEX idx_after_sale_order ON shop.after_sale_requests(order_id);

-- ============================================
-- customer_service schema (AI客服 — 保持兼容)
-- ============================================
CREATE SCHEMA IF NOT EXISTS customer_service;

CREATE TABLE IF NOT EXISTS customer_service.conversations (
    conversation_id VARCHAR(36) PRIMARY KEY,
    user_id         VARCHAR(128) NOT NULL,
    title           TEXT DEFAULT '',
    status          VARCHAR(16) DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customer_service.messages (
    id              SERIAL PRIMARY KEY,
    conversation_id VARCHAR(36) NOT NULL REFERENCES customer_service.conversations(conversation_id),
    role            VARCHAR(16) NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON customer_service.messages(conversation_id, turn_number);

-- ============================================
-- 种子数据: 分类
-- ============================================
INSERT INTO shop.categories (id, name, parent_id, sort_order) VALUES
(1, '智能门锁',    NULL, 1),
(2, '智能摄像头',  NULL, 2),
(3, '智能网关',    NULL, 3),
(4, '智能照明',    NULL, 4),
(5, '传感器',      NULL, 5);

-- 子分类
INSERT INTO shop.categories (id, name, parent_id, sort_order) VALUES
(6, '指纹门锁',  1, 1),
(7, '人脸识别锁', 1, 2),
(8, '室内摄像头', 2, 1),
(9, '室外摄像头', 2, 2),
(10, '网关套装',  3, 1);

-- ============================================
-- 种子数据: 商品 (12个)
-- ============================================
INSERT INTO shop.products (id, name, description, price, image_url, stock, category_id, status) VALUES
(1,  'X1 智能门锁',         '指纹+密码+App开锁，C级锁芯，续航12个月',          1599.00, '/static/img/x1.jpg',     100, 6,  'on_sale'),
(2,  'X2 智能门锁 Pro',     '3D人脸识别+指纹+远程开锁，双电池续航',             2499.00, '/static/img/x2.jpg',     50,  7,  'on_sale'),
(3,  'C1 智能摄像头',       '1080P高清，AI人形检测，双向语音通话',              399.00,  '/static/img/c1.jpg',     200, 8,  'on_sale'),
(4,  'C2 室外摄像头',       'IP67防水，全彩夜视，智能报警',                     699.00,  '/static/img/c2.jpg',     80,  9,  'on_sale'),
(5,  'G2 智能网关',         'Zigbee+WiFi双模，支持128设备，本地化控制',         699.00,  '/static/img/g2.jpg',     60,  10, 'on_sale'),
(6,  'L1 智能灯泡',         '1600万色，语音调光，定时开关',                     79.00,   '/static/img/l1.jpg',     500, 4,  'on_sale'),
(7,  'L2 灯带套装',         '5米RGB灯带，音乐律动，场景联动',                   149.00,  '/static/img/l2.jpg',     150, 4,  'on_sale'),
(8,  'S1 人体传感器',       '红外检测，5秒响应，低功耗待机2年',                  59.00,   '/static/img/s1.jpg',     300, 5,  'on_sale'),
(9,  'S2 门窗传感器',       '开合检测，即时推送，免工具安装',                    49.00,   '/static/img/s2.jpg',     250, 5,  'on_sale'),
(10, 'S3 温湿度传感器',     '高精度传感，历史曲线，场景联动',                    69.00,   '/static/img/s3.jpg',     200, 5,  'on_sale'),
(11, 'X1 门锁配件包',       '原装电池+备用钥匙+安装工具',                       99.00,   '/static/img/x1kit.jpg',  300, 6,  'off_sale'),
(12, 'G2 Pro 智能网关',     '多协议网关旗舰版，支持256设备',                    999.00,  '/static/img/g2pro.jpg',  0,   10, 'on_sale');

-- ============================================
-- 种子数据: 用户
-- 密码: admin123 / user123 / lisi123 (bcrypt)
-- ============================================
INSERT INTO shop.users (id, email, password, nickname, role, address) VALUES
(1, 'admin@shop.local',   '$2b$12$3MMnSyWPqKS9aWR/.3lWp.hT05X3k9CYTBj/kOaHfuR8nWp1L8ydS', 'Admin',  'admin', ''),
(2, 'zhangsan@shop.local','$2b$12$oP5EZ7VnOLeuEJ/Xbs/9BOQrd80tQoMynH5/NgyoSGIcEyaUQd2NK', '张三',   'user',  '广东省广州市天河区体育西路123号'),
(3, 'lisi@shop.local',    '$2b$12$TyeYDVgBObazYeKJdHmYt.rDyWrYbGyzfkY5rfXtLxmb2VYZG1.vS', '李四',   'user',  '北京市朝阳区望京街道456号');

-- ============================================
-- 种子数据: 订单 (含 paid + pending)
-- ============================================
INSERT INTO shop.orders (id, user_id, total_amount, status, address, created_at, paid_at) VALUES
(1001, 2, 1599.00, 'paid',    '广东省广州市天河区体育西路123号', '2024-05-01 10:30:00+08', '2024-05-01 10:32:00+08'),
(1002, 3, 399.00,  'paid',    '北京市朝阳区望京街道456号',     '2024-05-03 14:20:00+08', '2024-05-03 14:22:00+08'),
(1003, 2, 748.00,  'pending', '广东省广州市天河区体育西路123号', '2024-06-15 09:00:00+08', NULL);

-- 订单明细
INSERT INTO shop.order_items (id, order_id, product_id, product_name, price, quantity) VALUES
(1, 1001, 1, 'X1 智能门锁',    1599.00, 1),
(2, 1002, 3, 'C1 智能摄像头',  399.00,  1),
(3, 1003, 3, 'C1 智能摄像头',  399.00,  1),
(4, 1003, 6, 'L1 智能灯泡',    79.00,   2);

-- 支付记录
INSERT INTO shop.payment_records (order_id, amount, method) VALUES
(1001, 1599.00, 'mock'),
(1002, 399.00,  'mock');

-- ============================================
-- 种子数据: 物流
-- ============================================
INSERT INTO shop.logistics_records (order_id, tracking_number, carrier, status, current_location, estimated_delivery, timeline) VALUES
(1001, 'SF1234567890', '顺丰快递', 'delivered', '广州天河派送站',
 '2024-05-03 18:00:00+08',
 '[
   {"time":"2024-05-01 12:00","status":"已揽件","location":"广州天河营业点"},
   {"time":"2024-05-02 08:30","status":"运输中","location":"广州分拣中心"},
   {"time":"2024-05-03 06:00","status":"到达派送点","location":"广州天河派送站"},
   {"time":"2024-05-03 10:15","status":"派送中","location":""},
   {"time":"2024-05-03 14:30","status":"已签收","location":"本人签收"}
 ]'::jsonb),

(1002, 'JD9876543210', '京东物流', 'in_transit', '北京分拣中心',
 '2024-05-06 18:00:00+08',
 '[
   {"time":"2024-05-03 16:00","status":"已揽件","location":"北京朝阳营业点"},
   {"time":"2024-05-04 02:00","status":"运输中","location":"北京分拣中心"}
 ]'::jsonb);

-- ============================================
-- 种子数据: 售后
-- ============================================
INSERT INTO shop.after_sale_requests (id, user_id, order_id, type, reason, status, created_at, updated_at) VALUES
(1, 2, 1001, 'return',  '门锁面板有轻微划痕，不满意',         'completed', '2024-05-10 09:30:00+08', '2024-05-14 18:30:00+08'),
(2, 3, 1002, 'refund',  '摄像头频繁断连，无法正常使用',       'pending',   '2024-05-08 16:20:00+08', '2024-05-08 16:20:00+08'),
(3, 3, 1002, 'exchange','误购C1，想换C2室外摄像头',            'rejected',  '2024-05-09 10:00:00+08', '2024-05-10 14:00:00+08');

-- Reset sequences
SELECT setval('shop.categories_id_seq',            (SELECT MAX(id) FROM shop.categories));
SELECT setval('shop.products_id_seq',              (SELECT MAX(id) FROM shop.products));
SELECT setval('shop.users_id_seq',                 (SELECT MAX(id) FROM shop.users));
SELECT setval('shop.orders_id_seq',                (SELECT MAX(id) FROM shop.orders));
SELECT setval('shop.order_items_id_seq',           (SELECT MAX(id) FROM shop.order_items));
SELECT setval('shop.logistics_records_id_seq',     (SELECT MAX(id) FROM shop.logistics_records));
SELECT setval('shop.after_sale_requests_id_seq',   (SELECT MAX(id) FROM shop.after_sale_requests));
