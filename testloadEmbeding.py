from dotenv import load_dotenv

load_dotenv()

from hello_agents.memory.embedding import get_text_embedder

try:
    embedder = get_text_embedder()
    print(f"✅ Ollama Embedding 模型加载成功！")
    print(f"📊 模型类型: {type(embedder).__name__}")
    print(f"️ 模型名称: {embedder.model_name}")
    print(f"📏 向量维度: {embedder.dimension}")

    # 测试编码
    test_texts = ["测试中文文本", "AI Agent开发框架"]
    vecs = embedder.encode(test_texts)
    print(f"✨ 批量编码成功，生成 {len(vecs)} 个向量")
    print(f" 每个向量维度: {len(vecs[0])}")

except Exception as e:
    print(f"❌ 错误: {e}")
    import traceback

    traceback.print_exc()
