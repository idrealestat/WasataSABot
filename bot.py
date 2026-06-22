def search_youtube(query, api_key, max_results=3):
    try:
        youtube = build('youtube', 'v3', developerKey=api_key)
        request = youtube.search().list(
            part='snippet',
            q=query + " تعليمي شرح",
            type='video',
            maxResults=max_results,
            order='relevance'
        )
        response = request.execute()
        results = []
        for item in response['items']:
            video_id = item['id']['videoId']
            title = item['snippet']['title']
            url = f"https://www.youtube.com/watch?v={video_id}"
            results.append({'title': title, 'url': url})
        return results
    except Exception as e:
        logger.error(f"❌ خطأ في البحث عن يوتيوب: {e}")
        return []
