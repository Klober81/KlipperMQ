// Timed bookmark echo on the motion clock
//
// Copyright (C) 2026  Rob Niccum <klober@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // move_alloc
#include "board/irq.h" // irq_disable
#include "command.h" // DECL_COMMAND
#include "compiler.h" // container_of
#include "sched.h" // sched_add_timer

DECL_CONSTANT("BOOKMARK", 1);

struct bookmark_s {
    struct timer timer;
    struct move_queue_head mq;
};

struct bookmark_move {
    struct move_node node;
    uint32_t waketime, seq;
};

static struct bookmark_s bookmark;

static uint_fast8_t
bookmark_event(struct timer *t)
{
    struct bookmark_s *b = container_of(t, struct bookmark_s, timer);
    struct move_node *mn = move_queue_pop(&b->mq);
    struct bookmark_move *m = container_of(mn, struct bookmark_move, node);
    sendf("bookmark_echo clock=%u seq=%u", m->waketime, m->seq);
    move_free(m);
    if (move_queue_empty(&b->mq))
        return SF_DONE;
    struct move_node *nn = move_queue_first(&b->mq);
    struct bookmark_move *n = container_of(nn, struct bookmark_move, node);
    b->timer.waketime = n->waketime;
    return SF_RESCHEDULE;
}

void
command_queue_bookmark(uint32_t *args)
{
    struct bookmark_move *m = move_alloc();
    m->waketime = args[0];
    m->seq = args[1];
    irq_disable();
    int first = move_queue_push(&m->node, &bookmark.mq);
    irq_enable();
    if (!first)
        return;
    sched_del_timer(&bookmark.timer);
    bookmark.timer.func = bookmark_event;
    bookmark.timer.waketime = m->waketime;
    sched_add_timer(&bookmark.timer);
}
DECL_COMMAND(command_queue_bookmark, "queue_bookmark clock=%u seq=%u");

void
bookmark_init(void)
{
    move_queue_setup(&bookmark.mq, sizeof(struct bookmark_move));
    bookmark.timer.func = bookmark_event;
}
DECL_INIT(bookmark_init);

void
bookmark_shutdown(void)
{
    move_queue_clear(&bookmark.mq);
}
DECL_SHUTDOWN(bookmark_shutdown);
