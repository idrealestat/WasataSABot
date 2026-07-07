def get_section_response(user_message: str, section: str) -> str:
    """توليد رد تفصيلي لقسم معين، مع الالتزام الصارم بالمصادر الـ16 ونسخ النص الحرفي."""
    
    # تعليمات محددة لكل قسم مع التأكيد على النسخ الحرفي والمصادر
    section_prompts = {
        "source": """🔴 **مهمتك:** نسخ النص الحرفي من المصادر الرسمية مع ذكر المصدر والرابط.
- ابحث في المصادر الـ16 المذكورة.
- انسخ النص الرسمي بين علامتي تنصيص كما هو، دون تغيير أو اختصار.
- اذكر المصدر (مثل: الهيئة العامة للعقار) والرابط بعد كل اقتباس.
- إذا وجدت أكثر من مصدر، اذكرها جميعاً مع نصوصها.
- **إذا لم تجد أي نص حرفي في المصادر الـ16، قل: "لا توجد معلومات في المصادر المعتمدة" ولا تختلق.**
- لا تخرج عن المصادر الـ16 بأي حال.""",
        
        "requirements": """🔴 **مهمتك:** سرد المتطلبات (المستندات، التراخيص، الإجراءات) من المصادر الـ16 حرفياً.
- اعتمد على المصادر الـ16 فقط.
- انسخ النص الحرفي من المصدر مع ذكر المصدر والرابط.
- إذا لم تجد، قل: "لا توجد معلومات في المصادر المعتمدة".
- لا تختلق أو تفترض.""",
        
        "conditions": """🔴 **مهمتك:** سرد الشروط القانونية والتنظيمية من المصادر الـ16 حرفياً.
- اعتمد على المصادر الـ16 فقط.
- انسخ النص الحرفي مع ذكر المصدر والرابط.
- إذا لم تجد، قل: "لا توجد معلومات في المصادر المعتمدة".
- لا تختلق أو تفترض.""",
        
        "steps": """🔴 **مهمتك:** سرد الخطوات العملية من المصادر الـ16 حرفياً.
- اعتمد على المصادر الـ16 فقط.
- انسخ النص الحرفي مع ذكر المصدر والرابط.
- إذا لم تجد، قل: "لا توجد معلومات في المصادر المعتمدة".
- لا تختلق أو تفترض.""",
        
        "procedures": """🔴 **مهمتك:** سرد الجهات المعنية والرسوم من المصادر الـ16 حرفياً.
- اعتمد على المصادر الـ16 فقط.
- انسخ النص الحرفي مع ذكر المصدر والرابط.
- إذا لم تجد، قل: "لا توجد معلومات في المصادر المعتمدة".
- لا تختلق أو تفترض."""
    }
    
    instruction = section_prompts.get(section, "انسخ النص الحرفي من المصادر الـ16 مع ذكر المصدر والرابط. إذا لم تجد، قل 'لا توجد معلومات'.")
    
    system_prompt = f"""
أنت خبير عقاري سعودي، ومصدرك الوحيد هو المصادر الـ16 المذكورة في البرومبت الأساسي.
المستخدم يسأل عن: {user_message}

{instruction}

🔴 **تذكير إلزامي:**
- المصادر الـ16 هي فقط: الهيئة العامة للعقار، منصة إيجار، منصة سكني، البلديات، وزارة الإعلام، الجريدة الرسمية، الحسابات الرسمية، نظام الوساطة، اللائحة التنظيمية، عقار، بيوت، ديل، وصلت، حراج، السجل العقاري، بوابة النطاقات.
- **لا تخرج عن هذه المصادر تحت أي ظرف.**
- **انسخ النص الحرفي كما هو، ولا تلخص أو تعيد صياغة.**
- **إذا لم تجد المعلومة في هذه المصادر، قل فقط: "لا توجد معلومات في المصادر المعتمدة".**
- **لا تختلق، لا تفترض، لا تستخدم معرفتك العامة.**
"""
    
    # المحاولة الأولى: النموذج الأقوى (كما كان يعمل في البداية)
    try:
        logger.info(f"⚡ توليد قسم: {section} (محاولة 1 - النموذج الأقوى)")
        response = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.1,  # خفض الحرارة للالتزام بالنص الحرفي
            max_tokens=1200
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل النموذج الأقوى: {e}")
    
    # المحاولة الثانية: النموذج الأصغر
    try:
        logger.info(f"🔄 محاولة 2 - النموذج الأصغر")
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.1,
            max_tokens=1200
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل النموذج الأصغر: {e}")
    
    # المحاولة الثالثة: Gemini
    try:
        logger.info(f"🔄 محاولة 3 - Gemini")
        response = client_gemini.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.1,
            max_tokens=1200
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل Gemini: {e}")
    
    # المحاولة الرابعة: OpenRouter إن وجد
    if client_openrouter:
        try:
            logger.info(f"🔄 محاولة 4 - OpenRouter")
            response = client_openrouter.chat.completions.create(
                model="meta-llama/llama-3.1-8b-instruct",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.1,
                max_tokens=1200
            )
            reply = response.choices[0].message.content
            if not is_api_error(reply):
                return reply
        except Exception as e:
            logger.warning(f"⚠️ فشل OpenRouter: {e}")
    
    return f"❌ عذراً، لم أتمكن من استرجاع تفاصيل '{section}' بسبب مشكلة تقنية. يرجى المحاولة لاحقاً."
