import os,time,logging,requests,json,uuid,concurrent.futures,threading,base64,io
from io import BytesIO
from itertools import chain
from PIL import Image
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, Response, stream_with_context, render_template # Import render_template
from werkzeug.middleware.proxy_fix import ProxyFix
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from flask_cors import CORS  # 添加这行
os.environ['TZ'] = 'Asia/Shanghai'
time.tzset()
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
API_ENDPOINT = "https://openrouter.ai/api/v1/auth/key"
TEST_MODEL_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
def requests_session_with_retries(
    retries=3, backoff_factor=0.3, status_forcelist=(500, 502, 504)
):
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=1000,
        pool_maxsize=10000,
        pool_block=False
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
session = requests_session_with_retries()
app = Flask(__name__)
CORS(app)  # 启用CORS支持
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
models = {
    "text": [],
    "free_text": []
}
key_status = {
    "invalid": [],
    "free": [],
    "unverified": [],
    "valid": []
}
executor = concurrent.futures.ThreadPoolExecutor(max_workers=10000)
model_key_indices = {}
request_timestamps = []
token_counts = []
request_timestamps_day = []
token_counts_day = []
data_lock = threading.Lock()
def get_credit_summary(api_key):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = session.get(API_ENDPOINT, headers=headers, timeout=5)
            response.raise_for_status()
            data = response.json().get("data", {})
            
            # 解析OpenRouter返回的数据
            usage = data.get("usage", 0)
            limit = data.get("limit")
            limit_remaining = data.get("limit_remaining")
            is_free_tier = data.get("is_free_tier", False)
            rate_limit = data.get("rate_limit", {})
            
            # 如果limit为None，使用limit_remaining作为total_balance
            total_balance = limit_remaining if limit_remaining is not None else (
                limit - usage if limit is not None else float('inf')
            )
            
            logging.info(f"获取额度，API Key：{api_key}，当前额度: {total_balance}, "
                        f"使用量: {usage}, 限额: {limit}, 剩余限额: {limit_remaining}, "
                        f"是否免费用户: {is_free_tier}, "
                        f"速率限制: {rate_limit}")
            
            return {
                "total_balance": float(total_balance),
                "usage": usage,
                "limit": limit,
                "limit_remaining": limit_remaining,
                "is_free_tier": is_free_tier,
                "rate_limit": rate_limit
            }
        except requests.exceptions.Timeout as e:
            logging.error(f"获取额度信息超时，API Key：{api_key}，尝试次数：{attempt+1}/{max_retries}，错误信息：{e}")
            if attempt >= max_retries - 1:
                logging.error(f"获取额度信息失败，API Key：{api_key}，所有重试次数均已失败")
                return None
        except requests.exceptions.RequestException as e:
            logging.error(f"获取额度信息失败，API Key：{api_key}，错误信息：{e}")
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 402:
                logging.warning(f"API Key {api_key} 额度不足")
            return None
        except (KeyError, ValueError, TypeError) as e:
            logging.error(f"解析额度信息失败，API Key：{api_key}，错误信息：{e}")
            return None
FREE_MODEL_TEST_KEY = (
    "sk-bmjbjzleaqfgtqfzmcnsbagxrlohriadnxqrzfocbizaxukw"
)
def test_model_availability(api_key, model_name, model_type="chat"):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    try:
        payload = {
            "model": model_name, 
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
            "stream": False
        }
        response = session.post(
            TEST_MODEL_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=5
        )
        return response.status_code in [200, 429, 402]
    except requests.exceptions.RequestException as e:
        logging.error(
            f"测试模型 {model_name} 可用性失败，"
            f"API Key：{api_key}，错误信息：{e}"
        )
        return False
def process_image_url(image_url, response_format=None):
    if not image_url:
        return {"url": ""}
    if response_format == "b64_json":
        try:
            response = session.get(image_url, stream=True)
            response.raise_for_status()
            image = Image.open(response.raw)
            buffered = io.BytesIO()
            image.save(buffered, format="PNG")
            img_str = base64.b64encode(buffered.getvalue()).decode()
            return {"b64_json": img_str}
        except Exception as e:
            logging.error(f"图片转base64失败: {e}")
            return {"url": image_url}
    return {"url": image_url}
def create_base64_markdown_image(image_url):
    try:
        response = session.get(image_url, stream=True)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content))
        new_size = tuple(dim // 4 for dim in image.size)
        resized_image = image.resize(new_size, Image.LANCZOS)
        buffered = BytesIO()
        resized_image.save(buffered, format="PNG")
        base64_encoded = base64.b64encode(buffered.getvalue()).decode('utf-8')
        markdown_image_link = f"![](data:image/png;base64,{base64_encoded})"
        logging.info("Created base64 markdown image link.")
        return markdown_image_link
    except Exception as e:
        logging.error(f"Error creating markdown image: {e}")
        return None
def extract_user_content(messages):
    user_content = ""
    for message in messages:
        if message["role"] == "user":
            if isinstance(message["content"], str):
                user_content += message["content"] + " "
            elif isinstance(message["content"], list):
                for item in message["content"]:
                    if isinstance(item, dict) and item.get("type") == "text":
                        user_content += item.get("text", "") + " "
    return user_content.strip()
def get_siliconflow_data(model_name, data):
    siliconflow_data = {
        "model": model_name,
        "prompt": data.get("prompt") or "",
    }
    if model_name == "black-forest-labs/FLUX.1-pro":
        siliconflow_data.update({
            "width": max(256, min(1440, (data.get("width", 1024) // 32) * 32)),
            "height": max(256, min(1440, (data.get("height", 768) // 32) * 32)),
            "prompt_upsampling": data.get("prompt_upsampling", False),
            "image_prompt": data.get("image_prompt"),
            "steps": max(1, min(50, data.get("steps", 20))),
            "guidance": max(1.5, min(5, data.get("guidance", 3))),
            "safety_tolerance": max(0, min(6, data.get("safety_tolerance", 2))),
            "interval": max(1, min(4, data.get("interval", 2))),
            "output_format": data.get("output_format", "png")
        })
        seed = data.get("seed")
        if isinstance(seed, int) and 0 < seed < 9999999999:
            siliconflow_data["seed"] = seed
    else:
        siliconflow_data.update({
            "image_size": data.get("image_size", "1024x1024"),
            "prompt_enhancement": data.get("prompt_enhancement", False)
        })
        seed = data.get("seed")
        if isinstance(seed, int) and 0 < seed < 9999999999:
            siliconflow_data["seed"] = seed
        if model_name not in ["black-forest-labs/FLUX.1-schnell", "Pro/black-forest-labs/FLUX.1-schnell"]:
            siliconflow_data.update({
                "batch_size": max(1, min(4, data.get("n", 1))),
                "num_inference_steps": max(1, min(50, data.get("steps", 20))),
                "guidance_scale": max(0, min(100, data.get("guidance_scale", 7.5))),
                "negative_prompt": data.get("negative_prompt")
            })
    valid_sizes = ["1024x1024", "512x1024", "768x512", "768x1024", "1024x576", "576x1024", "960x1280", "720x1440", "720x1280"]
    if "image_size" in siliconflow_data and siliconflow_data["image_size"] not in valid_sizes:
        siliconflow_data["image_size"] = "1024x1024"
    return siliconflow_data
def refresh_models():
    global models
    models["text"] = get_all_models(FREE_MODEL_TEST_KEY, "chat")
    models["free_text"] = [model for model in models["text"] if model.endswith(":free")]
    
    ban_models = []
    ban_models_str = os.environ.get("BAN_MODELS")
    if ban_models_str:
        try:
            ban_models = json.loads(ban_models_str)
            if not isinstance(ban_models, list):
                logging.warning("环境变量 BAN_MODELS 格式不正确，应为 JSON 数组。")
                ban_models = []
        except json.JSONDecodeError:
            logging.warning("环境变量 BAN_MODELS JSON 解析失败，请检查格式。")
    
    models["text"] = [model for model in models["text"] if model not in ban_models]
    models["free_text"] = [model for model in models["free_text"] if model not in ban_models]
    
    logging.info(f"所有text模型列表：{models['text']}")
    logging.info(f"免费text模型列表：{models['free_text']}")
def load_keys():
    global key_status
    for status in key_status:
        key_status[status] = []
    keys_str = os.environ.get("KEYS")
    if not keys_str:
        logging.warning("环境变量 KEYS 未设置。")
        return
    test_model = os.environ.get("TEST_MODEL", "Pro/google/gemma-2-9b-it")
    unique_keys = list(set(key.strip() for key in keys_str.split(',')))
    os.environ["KEYS"] = ','.join(unique_keys)
    logging.info(f"加载的 keys：{unique_keys}")
    def process_key_with_logging(key):
        try:
            key_type = process_key(key, test_model)
            if key_type in key_status:
                key_status[key_type].append(key)
            return key_type
        except Exception as exc:
            logging.error(f"处理 KEY {key} 生成异常: {exc}")
            return "invalid"
    with concurrent.futures.ThreadPoolExecutor(max_workers=10000) as executor:
        futures = [executor.submit(process_key_with_logging, key) for key in unique_keys]
        concurrent.futures.wait(futures)
    for status, keys in key_status.items():
        logging.info(f"{status.capitalize()} KEYS: {keys}")
    global invalid_keys_global, free_keys_global, unverified_keys_global, valid_keys_global
    invalid_keys_global = key_status["invalid"]
    free_keys_global = key_status["free"]
    unverified_keys_global = key_status["unverified"]
    valid_keys_global = key_status["valid"]
def process_key(key, test_model):
    credit_summary = get_credit_summary(key)
    if credit_summary is None:
        return "invalid"
    else:
        total_balance = credit_summary.get("total_balance", 0)
        if total_balance <= 0.03:
            return "free"
        else:
            if test_model_availability(key, test_model):
                return "valid"
            else:
                return "unverified"
def get_all_models(api_key, sub_type):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    try:
        response = session.get(
            MODELS_ENDPOINT,
            headers=headers
        )
        response.raise_for_status()
        data = response.json()
        if (
            isinstance(data, dict) and
            'data' in data and
            isinstance(data['data'], list)
        ):
            models_list = []
            for model in data['data']:
                if isinstance(model, dict):
                    model_id = model.get("id")
                    if model_id:
                        models_list.append(model_id)
                        # 如果模型有免费版本，添加免费版本到列表中
                        if not model_id.endswith(":free"):
                            models_list.append(f"{model_id}:free")
            
            logging.info(
                f"获取模型列表成功，"
                f"共{len(models_list)}个模型"
            )
            return models_list
        else:
            logging.error("获取模型列表失败：响应数据格式不正确")
            return []
    except requests.exceptions.RequestException as e:
        logging.error(
            f"获取模型列表失败，"
            f"API Key：{api_key}，错误信息：{e}"
        )
        return []
    except (KeyError, TypeError) as e:
        logging.error(
            f"解析模型列表失败，"
            f"API Key：{api_key}，错误信息：{e}"
        )
        return []
def determine_request_type(model_name, model_list, free_model_list):
    if model_name.endswith(":free"):
        return "free"
    elif model_name in model_list:
        return "paid"
    else:
        return "unknown"
def select_key(request_type, model_name):
    if request_type == "free":
        available_keys = (
            free_keys_global +
            unverified_keys_global +
            valid_keys_global
        )
    elif request_type == "paid":
        available_keys = unverified_keys_global + valid_keys_global
    else:
        available_keys = (
            free_keys_global +
            unverified_keys_global +
            valid_keys_global
        )
    if not available_keys:
        return None
    current_index = model_key_indices.get(model_name, 0)
    for _ in range(len(available_keys)): # Corrected line: _in changed to _
        key = available_keys[current_index % len(available_keys)]
        current_index += 1
        if key_is_valid(key, request_type):
            model_key_indices[model_name] = current_index
            return key
        else:
            logging.warning(
                f"KEY {key} 无效或达到限制，尝试下一个 KEY"
            )
    model_key_indices[model_name] = 0
    return None
def key_is_valid(key, request_type):
    if request_type == "invalid":
        return False
    credit_summary = get_credit_summary(key)
    if credit_summary is None:
        return False
    total_balance = credit_summary.get("total_balance", 0)
    if request_type == "free":
        return True
    elif request_type == "paid" or request_type == "unverified": #Fixed typo here
        return total_balance > 0
    else:
        return False
def check_authorization(request):
    authorization_key = os.environ.get("AUTHORIZATION_KEY")
    if not authorization_key:
        logging.warning("环境变量 AUTHORIZATION_KEY 未设置，此时无需鉴权即可使用，建议进行设置后再使用。")
        return True
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        logging.warning("请求头中缺少 Authorization 字段。")
        return False
    if auth_header != f"Bearer {authorization_key}":
        logging.warning(f"无效的 Authorization 密钥：{auth_header}")
        return False
    return True

def obfuscate_key(key):
    if not key:
        return "****"
    prefix_length = 6
    suffix_length = 4
    if len(key) <= prefix_length + suffix_length:
        return "****" # If key is too short, just mask it all
    prefix = key[:prefix_length]
    suffix = key[-suffix_length:]
    masked_part = "*" * (len(key) - prefix_length - suffix_length)
    return prefix + masked_part + suffix

scheduler = BackgroundScheduler()
scheduler.add_job(load_keys, 'interval', hours=1)
scheduler.remove_all_jobs()
scheduler.add_job(refresh_models, 'interval', hours=1)

@app.route('/')
def index():
    current_time = time.time()
    one_minute_ago = current_time - 60
    one_day_ago = current_time - 86400
    with data_lock:
        while request_timestamps and request_timestamps[0] < one_minute_ago:
            request_timestamps.pop(0)
            token_counts.pop(0)
        rpm = len(request_timestamps)
        tpm = sum(token_counts)
    with data_lock:
        while request_timestamps_day and request_timestamps_day[0] < one_day_ago:
            request_timestamps_day.pop(0)
            token_counts_day.pop(0)
        rpd = len(request_timestamps_day)
        tpd = sum(token_counts_day)

    key_balances = []
    all_keys = list(chain(*key_status.values())) # Get all keys from all statuses
    with concurrent.futures.ThreadPoolExecutor(max_workers=10000) as executor:
        future_to_key = {executor.submit(get_credit_summary, key): key for key in all_keys}
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                credit_summary = future.result()
                balance = credit_summary.get("total_balance") if credit_summary else "获取失败"
                key_balances.append({"key": obfuscate_key(key), "balance": balance})
            except Exception as exc:
                logging.error(f"获取 KEY {obfuscate_key(key)} 余额信息失败: {exc}")
                key_balances.append({"key": obfuscate_key(key), "balance": "获取失败"})


    return render_template('index.html', rpm=rpm, tpm=tpm, rpd=rpd, tpd=tpd, key_balances=key_balances) # Render template instead of jsonify

@app.route('/models', methods=['GET'])
@app.route('/v1/models', methods=['GET'])
def list_models():
    if not check_authorization(request):
        return jsonify({"error": "Unauthorized"}), 401
    detailed_models = []
    all_models = chain(
        models["text"]
    )
    for model in all_models:
        model_data = {
            "id": model,
            "object": "model",
            "created": 1678888888,
            "owned_by": "openai",
            "permission": [],
            "root": model,
            "parent": None
        }
        detailed_models.append(model_data)
        if "DeepSeek-R1" in model:
            detailed_models.append({
                "id": model + "-thinking",
                "object": "model",
                "created": 1678888888,
                "owned_by": "openai",
                "permission": [],
                "root": model + "-thinking",
                "parent": None
            })
            detailed_models.append({
                "id": model + "-openwebui",
                "object": "model",
                "created": 1678888888,
                "owned_by": "openai",
                "permission": [],
                "root": model + "-openwebui",
                "parent": None
            })
    response = jsonify({
        "success": True,
        "data": detailed_models
    })
    return response

@app.route('/v1/dashboard/billing/usage', methods=['GET'])
def billing_usage():
    if not check_authorization(request):
        return jsonify({"error": "Unauthorized"}), 401
    daily_usage = []
    return jsonify({
        "object": "list",
        "data": daily_usage,
        "total_usage": 0
    })
@app.route('/v1/dashboard/billing/subscription', methods=['GET'])
def billing_subscription():
    if not check_authorization(request):
        return jsonify({"error": "Unauthorized"}), 401
    keys = valid_keys_global + unverified_keys_global
    total_balance = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=10000
    ) as executor:
        futures = [
            executor.submit(get_credit_summary, key) for key in keys
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                credit_summary = future.result()
                if credit_summary:
                    total_balance += credit_summary.get("total_balance", 0)
            except Exception as exc:
                logging.error(f"获取额度信息生成异常: {exc}")
    return jsonify({
        "object": "billing_subscription",
        "access_until": int(datetime(9999, 12, 31).timestamp()),
        "soft_limit": 0,
        "hard_limit": total_balance,
        "system_hard_limit": total_balance,
        "soft_limit_usd": 0,
        "hard_limit_usd": total_balance,
        "system_hard_limit_usd": total_balance
    })
@app.route('/v1/chat/completions', methods=['POST'])
def handsome_chat_completions():
    try:
        if not check_authorization(request):
            return jsonify({"error": "Unauthorized"}), 401
        
        data = request.get_json()
        if not data or 'model' not in data:
            return jsonify({"error": "Invalid request data"}), 400
        
        model_name = data['model']
        if model_name not in models["text"]:
            if "DeepSeek-R1" in model_name and (model_name.endswith("-openwebui") or model_name.endswith("-thinking")):
                model_realname = model_name.replace("-thinking", "").replace("-openwebui", "")
            else:
                return jsonify({"error": "Invalid model"}), 400
        else:
            model_realname = model_name
            
        request_type = determine_request_type(
            model_realname,
            models["text"],
            models["free_text"]
        )
        
        api_key = select_key(request_type, model_name)
        if not api_key:
            return jsonify({
                "error": "No available API key for this request type or all keys have reached their limits"
            }), 429
            
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        start_time = time.time()
        try:
            response = requests.post(
                TEST_MODEL_ENDPOINT,
                headers=headers,
                json=data,
                stream=data.get("stream", False),
                timeout=60
            )
            response.raise_for_status()
            
            if response.status_code == 429:
                return jsonify(response.json()), 429
                
            if data.get("stream", False):
                return Response(
                    stream_with_context(generate_stream_response(
                        response, start_time, data, api_key, model_name
                    )),
                    content_type="text/event-stream"
                )
            else:
                response_json = response.json()
                end_time = time.time()
                total_time = end_time - start_time
                
                # 获取token使用情况
                usage = response_json.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                
                # 获取响应内容
                choices = response_json.get("choices", [])
                response_content = ""
                if choices and choices[0].get("message"):
                    response_content = choices[0]["message"].get("content", "")
                
                # 记录日志
                user_content = extract_user_content(data.get("messages", []))
                user_content_replaced = user_content.replace('\n', '\\n').replace('\r', '\\n')
                response_content_replaced = response_content.replace('\n', '\\n').replace('\r', '\\n')
                
                logging.info(
                    f"使用的key: {api_key}, "
                    f"提示token: {prompt_tokens}, "
                    f"输出token: {completion_tokens}, "
                    f"总共用时: {total_time:.4f}秒, "
                    f"使用的模型: {model_name}, "
                    f"用户的内容: {user_content_replaced}, "
                    f"输出的内容: {response_content_replaced}"
                )
                
                # 更新统计信息
                with data_lock:
                    request_timestamps.append(time.time())
                    token_counts.append(prompt_tokens + completion_tokens)
                    request_timestamps_day.append(time.time())
                    token_counts_day.append(prompt_tokens + completion_tokens)
                
                # 如果响应是空的，记录警告
                if not response_content:
                    logging.warning(f"模型 {model_name} 返回了空响应")
                
                # 直接返回原始响应
                return jsonify(response_json)
                
        except requests.exceptions.RequestException as e:
            logging.error(f"请求OpenRouter API失败: {str(e)}")
            return jsonify({"error": "Failed to connect to OpenRouter API", "details": str(e)}), 502
            
    except Exception as e:
        logging.error(f"处理请求时发生未预期的错误: {str(e)}")
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500

def generate_stream_response(response, start_time, data, api_key, model_name):
    first_chunk_time = None
    full_response_content = ""
    response_content = ""
    
    try:
        for chunk in response.iter_content(chunk_size=2048):
            if chunk:
                if first_chunk_time is None:
                    first_chunk_time = time.time()
                chunk_str = chunk.decode("utf-8")
                full_response_content += chunk_str
                yield chunk
                
                try:
                    for line in chunk_str.splitlines():
                        if line.startswith("data:"):
                            line_json = json.loads(line[5:].strip())
                            if (
                                "choices" in line_json and
                                len(line_json["choices"]) > 0 and
                                "delta" in line_json["choices"][0] and
                                "content" in line_json["choices"][0]["delta"]
                            ):
                                response_content += line_json["choices"][0]["delta"]["content"]
                except (json.JSONDecodeError, KeyError) as e:
                    logging.warning(f"解析流式响应chunk时发生错误: {str(e)}")
                    continue
                    
        end_time = time.time()
        total_time = end_time - start_time
        first_token_time = first_chunk_time - start_time if first_chunk_time else 0
        
        # 记录统计信息
        log_completion_stats(
            api_key, model_name, data, response_content,
            full_response_content, total_time, first_token_time
        )
        
    except Exception as e:
        logging.error(f"生成流式响应时发生错误: {str(e)}")
        raise

def log_request_stats(api_key, model_name, prompt_tokens, completion_tokens, total_time, user_content, response_content):
    """记录请求统计信息"""
    try:
        user_content_replaced = user_content.replace('\n', '\\n').replace('\r', '\\n')
        response_content_replaced = response_content.replace('\n', '\\n').replace('\r', '\\n')
        
        logging.info(
            f"使用的key: {api_key}, "
            f"提示token: {prompt_tokens}, "
            f"输出token: {completion_tokens}, "
            f"总共用时: {total_time:.4f}秒, "
            f"使用的模型: {model_name}, "
            f"用户的内容: {user_content_replaced}, "
            f"输出的内容: {response_content_replaced}"
        )
        
        with data_lock:
            request_timestamps.append(time.time())
            token_counts.append(prompt_tokens + completion_tokens)
            request_timestamps_day.append(time.time())
            token_counts_day.append(prompt_tokens + completion_tokens)
    except Exception as e:
        logging.error(f"记录请求统计信息时发生错误: {str(e)}")

def log_completion_stats(api_key, model_name, data, response_content, full_response_content, total_time, first_token_time):
    try:
        prompt_tokens = 0
        completion_tokens = 0
        
        for line in full_response_content.splitlines():
            if line.startswith("data:"):
                try:
                    line_json = json.loads(line[5:].strip())
                    if "usage" in line_json:
                        prompt_tokens = line_json["usage"].get("prompt_tokens", 0)
                        completion_tokens = line_json["usage"].get("completion_tokens", 0)
                except (json.JSONDecodeError, KeyError):
                    continue
                    
        user_content = extract_user_content(data.get("messages", []))
        user_content_replaced = user_content.replace('\n', '\\n').replace('\r', '\\n')
        response_content_replaced = response_content.replace('\n', '\\n').replace('\r', '\\n')
        
        logging.info(
            f"使用的key: {api_key}, "
            f"提示token: {prompt_tokens}, "
            f"输出token: {completion_tokens}, "
            f"首字用时: {first_token_time:.4f}秒, "
            f"总共用时: {total_time:.4f}秒, "
            f"使用的模型: {model_name}, "
            f"用户的内容: {user_content_replaced}, "
            f"输出的内容: {response_content_replaced}"
        )
        
        with data_lock:
            request_timestamps.append(time.time())
            token_counts.append(prompt_tokens + completion_tokens)
            request_timestamps_day.append(time.time())
            token_counts_day.append(prompt_tokens + completion_tokens)
            
    except Exception as e:
        logging.error(f"记录完成统计信息时发生错误: {str(e)}")

def process_normal_response(response, start_time, data, api_key, model_name):
    """处理普通（非流式）响应"""
    try:
        end_time = time.time()
        response_json = response.json()
        total_time = end_time - start_time
        
        # 获取token使用情况
        usage = response_json.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        
        # 获取响应内容
        choices = response_json.get("choices", [])
        response_content = ""
        if choices:
            message = choices[0].get("message", {})
            response_content = message.get("content", "")
            
        # 记录日志
        user_content = extract_user_content(data.get("messages", []))
        user_content_replaced = user_content.replace('\n', '\\n').replace('\r', '\\n')
        response_content_replaced = response_content.replace('\n', '\\n').replace('\r', '\\n')
        
        logging.info(
            f"使用的key: {api_key}, "
            f"提示token: {prompt_tokens}, "
            f"输出token: {completion_tokens}, "
            f"总共用时: {total_time:.4f}秒, "
            f"使用的模型: {model_name}, "
            f"用户的内容: {user_content_replaced}, "
            f"输出的内容: {response_content_replaced}"
        )
        
        # 更新统计信息
        with data_lock:
            request_timestamps.append(time.time())
            token_counts.append(prompt_tokens + completion_tokens)
            request_timestamps_day.append(time.time())
            token_counts_day.append(prompt_tokens + completion_tokens)
        
        # 如果响应是空的或没有内容，记录警告
        if not response_content:
            logging.warning(f"模型 {model_name} 返回了空响应")
            
        # 直接返回原始响应，保持与API的一致性
        return jsonify(response_json)
        
    except Exception as e:
        logging.error(f"处理普通响应时发生错误: {str(e)}")
        return jsonify({
            "error": "Response processing error",
            "details": str(e)
        }), 500

# 添加OPTIONS请求处理
@app.route('/models', methods=['OPTIONS'])
@app.route('/v1/models', methods=['OPTIONS'])
def handle_options():
    response = jsonify({})
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

if __name__ == '__main__':
    logging.info(f"环境变量：{os.environ}")
    load_keys()
    logging.info("程序启动时首次加载 keys 已执行")
    scheduler.start()
    logging.info("首次加载 keys 已手动触发执行")
    refresh_models()
    logging.info("首次刷新模型列表已手动触发执行")
    app.run(debug=False,host='0.0.0.0',port=int(os.environ.get('PORT', 7860)))
    
